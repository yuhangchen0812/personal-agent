"""真实加载事件与任务关联；可选连续反馈只生成待审阅候选，不恢复执行。"""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import re
from threading import RLock
from uuid import uuid4

from . import skills
from .skill_lifecycle import SkillLifecycleStore, _now
from .structured_output import strict_json


_active_learning = ContextVar("personal_agent_skill_learning", default=None)
LOAD_EVENTS = {"inline_loaded", "fork_started"}


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _text_sha(text):
    return _sha(text.encode("utf-8"))


def _task_path(files, task_id):
    if not isinstance(task_id, str) or not re.fullmatch(r"task_[a-f0-9]{32}", task_id):
        raise ValueError("Skill 任务记录 ID 无效。")
    return files._path(f".personal/skill-usage/tasks/{task_id}.json")


def load_skill_task(files, task_id):
    value = strict_json(_task_path(files, task_id).read_text(encoding="utf-8"))
    if (not isinstance(value, dict) or value.get("version") != 1 or value.get("kind") != "skill_task_use"
            or value.get("id") != task_id or value.get("project_root") != str(files.root)
            or value.get("status") not in {"loop_completed", "failed", "cancelled"}
            or not isinstance(value.get("uses"), list) or not value["uses"]):
        raise ValueError("Skill 任务记录格式或项目归属无效。")
    for use in value["uses"]:
        if not isinstance(use, dict):
            raise ValueError("任务中的 Skill 使用引用必须是对象。")
        event_id = use.get("event_id")
        if not isinstance(event_id, str) or not re.fullmatch(r"use_[a-f0-9]{32}", event_id):
            raise ValueError("任务中的 Skill 使用事件 ID 无效。")
        raw = files._path(f".personal/skill-usage/events/{event_id}.json").read_bytes()
        event = strict_json(raw.decode("utf-8"))
        if (not isinstance(event, dict) or _sha(raw) != use.get("event_sha256") or event.get("id") != event_id
                or event.get("project_root") != str(files.root) or event.get("event") not in LOAD_EVENTS
                or any(event.get(key) != use.get(key) for key in ("name", "source", "skill_path", "source_sha256", "event"))):
            raise ValueError("任务关联的真实 Skill 使用事件变化。")
    return value


def check_feedback_use(prepared):
    """校验可选任务引用；旧反馈没有引用时保持原语义。"""
    if "task_use" not in prepared:
        return
    files = SkillLifecycleStore()
    ref = prepared["task_use"]
    if not isinstance(ref, dict) or set(ref) != {"id", "sha256"}:
        raise ValueError("反馈任务引用格式无效。")
    task = load_skill_task(files, ref["id"])
    if (_sha(_task_path(files, ref["id"]).read_bytes()) != ref["sha256"] or task["status"] != "loop_completed"
            or task["task_sha256"] != _text_sha(prepared["task"]) or task["answer_sha256"] != _text_sha(prepared["answer"])
            or not any(use["source"] == "project" and use["name"] == prepared["skill_name"]
                and use["skill_path"] == prepared["skill_path"] and use["source_sha256"] == prepared["base_sha256"] for use in task["uses"])):
        raise ValueError("反馈与实际任务、回答或使用的 Skill 版本不一致。")


def list_skill_tasks(files, selector=None):
    rows = [load_skill_task(files, path.stem) for path in files._path(".personal/skill-usage/tasks").glob("*.json")]
    return sorted((row for row in rows if selector is None or selector == row["id"] or any(use["name"] == selector for use in row["uses"])),
        key=lambda row: (row["created_at"], row["id"]))


@contextmanager
def skill_learning_scope(learning):
    token = _active_learning.set(learning)
    try:
        yield learning
    finally:
        _active_learning.reset(token)


def observe_skill_event(event):
    learning = _active_learning.get()
    if learning is not None:
        learning.observe(event)


class SkillLearning:
    def __init__(self, jobs, *, session_name=None):
        self.jobs, self.files, self.session_name = jobs, SkillLifecycleStore(), session_name
        self.enabled, self.target, self.pending = False, None, None
        self.active, self.last = None, None
        self._lock = RLock()

    def enable(self, name=None):
        if self.jobs.window is not None or self.jobs.future is not None:
            raise ValueError("先关闭显式窗口并结算后台请求，再开启连续模式。")
        if name is not None:
            skills.get_project_skill(name)
        self.enabled, self.target, self.pending = True, name, None

    def disable(self):
        self.enabled, self.target, self.pending = False, None, None

    def reset(self):
        self.pending, self.active, self.last = None, None, None

    def describe(self):
        return {"enabled": self.enabled, "target": self.target, "pending_skill": self.pending["skill_name"] if self.pending else None}

    def begin(self, task):
        with self._lock:
            self.pending = None
            self.active = {"version": 1, "kind": "skill_task_use", "id": "task_" + uuid4().hex,
                "project_root": str(self.files.root), "created_at": _now(), "session_name": self.session_name,
                "task_sha256": _text_sha(task), "answer_sha256": None, "uses": []}

    def observe(self, event):
        if event["event"] not in LOAD_EVENTS:
            return
        with self._lock:
            if self.active is None:
                return
            raw = self.files._path(f".personal/skill-usage/events/{event['id']}.json").read_bytes()
            self.active["uses"].append({"event_id": event["id"], "event_sha256": _sha(raw),
                **{key: event.get(key) for key in ("name", "source", "skill_path", "source_sha256", "event")}})

    def bind_window(self, prepared):
        if self.last is None:
            return prepared
        candidate = {**prepared, "task_use": {"id": self.last["id"], "sha256": _sha(_task_path(self.files, self.last["id"]).read_bytes())}}
        try:
            check_feedback_use(candidate)
        except ValueError:
            return prepared  # 显式反馈仍可针对没实际加载目标技能的普通任务，不能伪造使用引用。
        return candidate

    def finish(self, status, messages=None, *, answer=None, allowed=True):
        with self._lock:
            task, self.active = self.active, None
        self.last = None
        if task is None or not task["uses"]:
            return None
        task.update(status=status, answer_sha256=_text_sha(answer) if answer is not None else None)
        path = _task_path(self.files, task["id"])
        self.files._write(path, task)
        self.last = load_skill_task(self.files, task["id"])
        if not (self.enabled and allowed and status == "loop_completed"):
            return task
        choices = {(use["name"], use["skill_path"], use["source_sha256"]) for use in task["uses"] if use["source"] == "project"
            and use["source_sha256"] is not None and (self.target is None or self.target == use["name"])}
        if len(choices) != 1:
            return task  # 多方法/多版本不猜反馈对象；可指定目标后进行下一次任务。
        name, path, version = choices.pop()
        prepared = self.jobs.evolution.prepare_feedback(name, messages, session_name=self.session_name)
        if prepared["skill_path"] != path or prepared["base_sha256"] != version:
            raise ValueError("任务使用的 Skill 版本已经变化，未建立连续反馈窗口。")
        self.pending = self.bind_window(prepared)
        if "task_use" not in self.pending:
            self.pending = None
            raise ValueError("实际使用证据不匹配，未建立连续反馈窗口。")
        return task

    def arm_pending(self, *, allowed=True):
        prepared, self.pending = self.pending, None
        if not allowed or prepared is None:
            raise ValueError("当前没有可提炼的连续窗口，或正处于 Plan。")
        if self.jobs.window is not None or self.jobs.future is not None:
            raise ValueError("已有显式窗口或后台请求，连续窗口不自动重试。")
        check_feedback_use(prepared)
        self.jobs.window = deepcopy(prepared)

    def submit_next(self, text, *, allowed=True):
        self.arm_pending(allowed=allowed)
        try:
            return self.jobs.submit_feedback(text)
        finally:
            self.jobs.window = None  # 拒绝/预算不足也消费一次，后续普通问题不会被误当旧反馈。
