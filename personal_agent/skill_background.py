"""一次性反馈窗口与单任务后台草稿；线程只请求模型，主线程结算和落盘。"""

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import datetime, timezone
from uuid import uuid4

from .model_errors import MODEL_API_ERRORS

from .skill_evolution import SkillEvolutionStore
from .skill_lifecycle import SkillLifecycleStore


def _now():
    return datetime.now(timezone.utc).isoformat()


def _worker(request, call_model):
    reported = None

    def observe(usage):
        nonlocal reported
        reported = usage

    try:
        return {"response": call_model(request, observe), "usage": reported, "error": None}
    except BaseException as error:
        detail = {"type": type(error).__name__}
        if isinstance(error, MODEL_API_ERRORS):
            detail["status_code"] = getattr(error, "status_code", None)
        else:
            detail["message"] = str(error)
        return {"response": None, "usage": reported, "error": detail}


class SkillDraftJobs:
    def __init__(self, call_model, record_usage, check_budget, *, model_settings: dict):
        self.evolution = SkillEvolutionStore()
        self.files = SkillLifecycleStore()
        self.call_model, self.record_usage, self.check_budget = call_model, record_usage, check_budget
        self.model_settings = dict(model_settings)
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="skill-draft")
        self.window = None
        self.future = None
        self.active = None
        self.request = None
        self.closed = False

    def _path(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch(r"job_[a-f0-9]{32}", job_id):
            raise ValueError("后台任务 ID 无效。")
        return self.files._path(f".personal/skill-jobs/{job_id}.json")

    def _save(self, job, *, new=False):
        self.files._write(self._path(job["id"]), job, new=new)

    def load(self, job_id):
        job = json.loads(self._path(job_id).read_text(encoding="utf-8"))
        if not isinstance(job, dict) or job.get("version") != 1 or job.get("id") != job_id or job.get("project_root") != str(self.evolution.root):
            raise ValueError("后台记录 ID、版本或项目归属不匹配。")
        if job.get("status") in ("running", "cancel_requested") and (self.active is None or self.active["id"] != job_id):
            job = {**job, "persisted_status": job["status"], "status": "not_resumed"}
        return job

    def list_jobs(self):
        return [self.load(path.stem) for path in sorted(self.files._path(".personal/skill-jobs").glob("*.json"))]

    def arm(self, name, messages, *, session_name=None):
        if self.closed or self.future is not None:
            raise ValueError("后台尚有未结算请求，暂不能开启新窗口。")
        if self.window is not None:
            raise ValueError("已有反馈窗口；先提交反馈或 /skill-learn off。")
        self.window = self.evolution.prepare_feedback(name, messages, session_name=session_name)
        return dict(self.window)

    def submit_feedback(self, text):
        if self.closed or self.window is None or self.future is not None:
            raise ValueError("没有可提交的反馈窗口，或后台任务尚未结算。")
        self.check_budget()  # 主线程先检查，未发出请求不计为缺失用量。
        feedback = self.evolution.save_feedback(self.window, text)
        self.window = None
        request = self.evolution.prepare_draft(feedback["id"])
        job = {"version": 1, "id": "job_" + uuid4().hex, "project_root": str(self.evolution.root),
               "created_at": _now(), "status": "running", "feedback_id": feedback["id"],
               "skill_name": feedback["skill_name"], "base_sha256": feedback["base_sha256"],
               "model_settings": self.model_settings, "request_sha256": hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
               "candidate_id": None, "usage": None, "logical_call_started": None}
        self._save(job, new=True)
        self.active = job
        self.request = request
        try:
            self.future = self.pool.submit(copy_context().run, _worker, request, self.call_model)
        except Exception:
            job.update(status="failed", error={"type": "SubmitFailed"}, finished_at=_now())
            self._save(job)
            self.active = None
            raise
        return dict(job)

    def cancel(self, job_id):
        if self.active is None or self.active["id"] != job_id:
            return self.load(job_id)  # 已结束的任务不撤回已保存候选。
        self.active["status"] = "cancel_requested"
        self.future.cancel()  # 尚未运行时可撤回；已发出的 SDK 请求仅作逻辑取消。
        self._save(self.active)
        return dict(self.active)

    def extract_now(self, hint: str = ""):
        """消费一次显式冻结窗口并等待结算；无提示时使用中性的主动提炼请求。"""
        if not isinstance(hint, str):
            raise ValueError("提炼提示必须是文本。")
        self.submit_feedback(hint.strip() or "用户主动请求提炼该完整任务中有依据、可复用的方法，未提供额外纠错反馈。")
        return self.poll(wait=True)

    def poll(self, *, wait=False):
        """仅由主线程调用；回收一次结果，记录一次用量，再决定能否保存候选。"""
        future, job, request = self.future, self.active, self.request
        if future is None or (not wait and not future.done()):
            return None
        try:
            result = None if future.cancelled() else future.result()
        except KeyboardInterrupt:
            self.cancel(job["id"])
            raise
        # 先摘掉 Future，后续落盘失败也不能重记用量或重复保存候选。
        self.future, self.active = None, None
        self.request = None
        job["logical_call_started"] = result is not None
        if result is not None:
            self.record_usage(result["usage"])
            job.update(usage=result["usage"], logical_call_started=True)
        if job["status"] == "cancel_requested" or result is None:
            job["status"] = "cancelled"
        elif result["error"] is not None:
            job.update(status="failed", error=result["error"])
        else:
            try:
                outcome = self.evolution.save_draft(job["feedback_id"], result["response"], model_name=self.model_settings.get("model"), request=request)
                if outcome["kind"] == "decision":
                    job.update(status="discarded", decision_id=outcome["id"])
                else:
                    job.update(status="completed", candidate_id=outcome["id"], decision_id=outcome.get("decision_id"))
            except (ValueError, OSError) as error:
                job.update(status="failed", error={"type": type(error).__name__, "message": str(error)})
        job["finished_at"] = _now()
        try:
            self._save(job)
        except (ValueError, OSError):
            raise RuntimeError(f"后台任务已结算，但回执保存失败；任务 {job['id']}，候选 {job['candidate_id'] or '无'}。不会自动重跑，请核对本地候选。") from None
        return job

    def reset(self):
        self.window = None
        if self.active is not None:
            self.cancel(self.active["id"])

    def close(self):
        self.closed = True
        try:
            try:
                self.reset()
            except (ValueError, OSError) as error:
                print(f"后台取消回执未保存（{type(error).__name__}），仍将结算已发出的请求。")
            if self.future is not None:
                print("正在等待已发出的后台请求结束并结算；取消任务不保存候选。")
                self.poll(wait=True)
        finally:
            self.pool.shutdown(wait=True, cancel_futures=True)


def format_skill_job(job):
    text = f"后台任务：{job['id']} [{job['status']}]；技能：{job['skill_name']}；反馈：{job['feedback_id']}"
    if job.get("candidate_id"):
        text += f"；候选：{job['candidate_id']}（当前状态用 /skill-candidates 查看）"
    if job.get("error"):
        text += "；错误：" + json.dumps(job["error"], ensure_ascii=False)
    if job.get("decision_id"):
        text += f"；决策：{job['decision_id']}"
    return text
