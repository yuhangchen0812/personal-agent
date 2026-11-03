"""显式任务反馈、补充经验候选与采纳记录；不自动修改活动技能。"""

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from . import skills
from .context import SUMMARY_PREFIX
from .session import validate_agent_messages
from .skill_decision import DECISION_PROMPT


DRAFT_PROMPT = DECISION_PROMPT
DIRECTORIES = {"feedback": "feedback", "candidate": "skill-candidates", "decision": "skill-decisions"}
PREFIXES = {"feedback": "fb_", "candidate": "cand_", "decision": "dec_"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_rating(rating: str) -> str:
    """评分只保存用户原标签，不推断量表、成功率或模型质量。"""
    if not isinstance(rating, str) or not rating.strip() or len(rating) > 64:
        raise ValueError("评分须为 1～64 字符的非空文本标签。")
    return rating.strip()


class SkillEvolutionStore:
    """记录固定在当前项目 .personal 下；ID 不接受路径，文件不授予执行权限。"""

    def __init__(self):
        self.root = skills.PROJECT_ROOT.resolve()

    def _path(self, kind: str, record_id: str) -> Path:
        if not isinstance(record_id, str) or not re.fullmatch(PREFIXES[kind] + r"[a-f0-9]{32}", record_id):
            raise ValueError("反馈或候选 ID 无效；请使用列表中完整的 ID。")
        directory = self.root / ".personal" / DIRECTORIES[kind]
        path = directory / f"{record_id}.json"
        if directory.resolve() != directory or path.resolve() != path:
            raise ValueError("反馈/候选路径不能通过链接跳出项目原目录。")
        return path

    def _save(self, record: dict, *, new: bool = False) -> None:
        path = self._path(record["kind"], record["id"])
        payload = (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path(record["kind"], record["id"])
        if new:
            created = False
            try:
                with path.open("xb") as target:
                    created = True
                    target.write(payload)
            except BaseException:
                if created:
                    path.unlink(missing_ok=True)
                raise
            return
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".record-", suffix=".tmp", delete=False) as target:
                temporary = Path(target.name)
                target.write(payload)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _new(self, kind: str, **fields) -> dict:
        return {"version": 1, "kind": kind, "id": PREFIXES[kind] + uuid4().hex,
                "project_root": str(self.root), "created_at": _now(), **fields}

    def _read(self, kind: str, record_id: str) -> dict:
        try:
            record = json.loads(self._path(kind, record_id).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValueError(f"无法读取{kind}记录（{type(error).__name__}）。") from None
        if not isinstance(record, dict) or record.get("version") not in ((1, 2) if kind == "candidate" else (1,)) or record.get("id") != record_id or record.get("kind") != kind or record.get("project_root") != str(self.root):
            raise ValueError("记录版本、ID、类型或项目归属不匹配。")
        if kind == "decision":
            if (record.get("action") not in ("add", "merge", "discard")
                    or record.get("requested_action") not in ("add", "merge", "discard")
                    or any(not isinstance(record.get(key), str) or not record[key] for key in ("feedback_id", "rationale", "decision_key"))):
                raise ValueError("技能决策记录缺少有效动作或来源。")
            return record
        required = ("created_at", "skill_name", "skill_path", "base_sha256")
        required += ("task", "answer", "feedback") if kind == "feedback" else ("feedback_id", "lesson", "rationale", "status")
        is_add = kind == "candidate" and record.get("version") == 2 and record.get("action") == "add"
        if is_add:
            required = tuple(key for key in required if key != "base_sha256") + ("description",)
        if kind == "candidate" and record.get("status") == "adopted":
            required += ("created_sha256", "adopted_at") if is_add else ("snapshot", "adopted_at")
        if any(not isinstance(record.get(key), str) or not record[key].strip() for key in required):
            raise ValueError("反馈/候选记录缺少必要的非空字符串字段。")
        if (is_add and record.get("base_sha256") is not None) or (not is_add and not re.fullmatch(r"[a-f0-9]{64}", record["base_sha256"])):
            raise ValueError("记录中的原技能版本摘要无效。")
        if kind == "candidate" and record["version"] == 2:
            source = record.get("source")
            if (record.get("action") not in ("add", "merge") or not isinstance(source, dict)
                    or any(not isinstance(source.get(key), str) or not source[key] for key in ("skill_name", "skill_path", "base_sha256"))
                    or not re.fullmatch(r"[a-f0-9]{64}", source["base_sha256"])):
                raise ValueError("新版候选动作或来源版本无效。")
        if kind == "candidate" and record["status"] not in {"pending", "adopted", "rejected"}:
            raise ValueError("候选状态无效。")
        if kind == "candidate" and not isinstance(record.get("generation", {}), dict):
            raise ValueError("候选生成记录必须是对象。")
        if kind == "feedback" and "rating" in record:
            _validate_rating(record["rating"])
        if kind == "feedback":
            from .skill_learning import check_feedback_use
            check_feedback_use(record)
        return record

    def feedback(self, record_id: str) -> dict:
        return self._read("feedback", record_id)

    def candidate(self, record_id: str) -> dict:
        return self._read("candidate", record_id)

    def list_records(self, kind: str) -> list[dict]:
        if kind not in DIRECTORIES:
            raise ValueError("未知记录类型。")
        directory = self.root / ".personal" / DIRECTORIES[kind]
        if directory.resolve() != directory:
            raise ValueError("记录目录不能使用链接。")
        records = [self._read(kind, path.stem) for path in directory.glob("*.json")]
        return sorted(records, key=lambda record: (record["created_at"], record["id"]))

    def record_feedback(self, name: str, feedback: str, messages: list[dict], *, session_name: str | None = None, rating: str | None = None) -> dict:
        return self.save_feedback(self.prepare_feedback(name, messages, session_name=session_name), feedback, rating=rating)

    def prepare_feedback(self, name: str, messages: list[dict], *, session_name: str | None = None) -> dict:
        """冻结最近完整任务、回答和目标版本；尚不写入反馈记录。"""
        history = validate_agent_messages(messages)
        starts = [index for index, message in enumerate(history) if message["role"] == "user"]
        if not starts or history[starts[-1]]["content"].startswith(SUMMARY_PREFIX):
            raise ValueError("请先完成一轮实际问答，再记录对该轮的反馈。")
        target = skills.get_project_skill(name)
        return self._new("feedback", skill_name=name, skill_path=target.file_path.relative_to(self.root).as_posix(),
                         base_sha256=_sha256(target.file_path.read_bytes()), task=history[starts[-1]]["content"],
                         answer=history[-1]["content"], session_name=session_name)

    def save_feedback(self, prepared: dict, feedback: str, *, rating: str | None = None) -> dict:
        """提交一次冻结窗口；期间目标版本变化则拒绝，不改绑新任务。"""
        if not isinstance(feedback, str) or not feedback.strip() or len(feedback) > 4000:
            raise ValueError("反馈须为 1～4000 字符的非空文本。")
        if rating is not None:
            rating = _validate_rating(rating)
        from .skill_learning import check_feedback_use
        check_feedback_use(prepared)
        target = self._target(prepared)
        if _sha256(target.file_path.read_bytes()) != prepared["base_sha256"]:
            raise ValueError("反馈窗口中的技能版本已变化，请重新开启窗口。")
        record = {**prepared, "feedback": feedback.strip(), **({"rating": rating} if rating is not None else {})}
        self._save(record, new=True)
        return record

    def _target(self, record: dict):
        target = skills.get_project_skill(record["skill_name"])
        if target.file_path.relative_to(self.root).as_posix() != record["skill_path"]:
            raise ValueError("目标技能路径已变化，请重新记录反馈。")
        return target

    def draft(self, feedback_id: str, call_model: Callable[[list[dict]], dict], *, model_name: str | None = None) -> dict:
        request = self.prepare_draft(feedback_id)
        return self.save_draft(feedback_id, call_model(request), model_name=model_name, request=request)

    def prepare_draft(self, feedback_id: str) -> list[dict]:
        """在主线程校验版本并固定独立请求；不执行模型或写候选。"""
        feedback = self.feedback(feedback_id)
        target = self._target(feedback)
        if _sha256(target.file_path.read_bytes()) != feedback["base_sha256"]:
            raise ValueError("技能版本已变化，请重新记录反馈和生成候选。")
        from .skill_decision import decision_catalog
        return [
            {"role": "system", "content": DRAFT_PROMPT},
            {"role": "user", "content": json.dumps({"feedback_record": feedback, "skill_instructions": target.prompt_template,
                                                   "known_skills": decision_catalog(self)}, ensure_ascii=False)},
        ]

    def save_draft(self, feedback_id: str, response: dict, *, model_name: str | None = None, request: list[dict] | None = None) -> dict:
        """模型返回后再次校验目标，再解析和保存 pending 候选。"""
        self.prepare_draft(feedback_id)
        feedback = self.feedback(feedback_id)
        if not isinstance(response, dict) or response.get("role") != "assistant" or response.get("tool_calls"):
            raise ValueError("候选必须是纯文本 assistant JSON，工具请求不会执行。")
        content = response.get("content")
        if not isinstance(content, str) or len(content) > 4000:
            raise ValueError("候选必须是不超过 4000 字符的 JSON 文本。")
        try:
            generated = json.loads(content)
        except ValueError:
            raise ValueError("候选不是有效 JSON。") from None
        if isinstance(generated, dict) and "action" in generated:
            from .skill_decision import save_decision
            return save_decision(self, feedback, generated, request, model_name)
        if (not isinstance(generated, dict) or set(generated) != {"lesson", "rationale"}
                or any(not isinstance(generated.get(key), str) or not generated[key].strip() or len(generated[key]) > limit
                       for key, limit in (("lesson", 2000), ("rationale", 1000)))):
            raise ValueError("候选须且只能包含非空 lesson（最多 2000 字符）和 rationale（最多 1000 字符）。")
        candidate = self._new("candidate", feedback_id=feedback_id, skill_name=feedback["skill_name"],
                              skill_path=feedback["skill_path"], base_sha256=feedback["base_sha256"],
                              lesson=re.sub(r"\s+", " ", generated["lesson"]).strip(), rationale=generated["rationale"].strip(),
                              status="pending", generation={"model": model_name, "prompt_version": "append-lesson-v1"})
        self._save(candidate, new=True)
        return candidate

    def describe(self, candidate_id: str) -> str:
        candidate = self.candidate(candidate_id)
        source = self.feedback(candidate["feedback_id"])
        change = "将创建新的项目 inline 技能；不会覆盖已有目录。" if candidate.get("action") == "add" else "将追加经验及来源标记，保留目标原方法和元信息。"
        return (f"候选：{candidate['id']} [{candidate['status']}]；动作：{candidate.get('action', 'merge')}\n目标：{candidate['skill_name']}（{candidate['skill_path']}）\n"
                f"生成记录：{candidate['created_at']}（UTC），模型：{candidate.get('generation', {}).get('model') or '未知'}\n"
                f"原版 SHA256：{candidate['base_sha256']}\n来源：{source['id']}，会话：{source.get('session_name') or '内存会话'}\n"
                f"任务：{source['task']}\n原回答：{source['answer']}\n用户反馈：{source['feedback']}\n"
                f"拟追加经验：{candidate['lesson']}\n理由：{candidate['rationale']}\n"
                f"采纳{change}本次展示不代表已验证效果；merge 可用 /skill-evals 查看独立回放报告，add 尚不支持成对回放。")

    def adopt(self, candidate_id: str) -> dict:
        candidate = self.candidate(candidate_id)
        if candidate["status"] == "adopted":
            return candidate  # 重复命令不重复追加。
        if candidate["status"] != "pending":
            raise ValueError("该候选已拒绝，不能采纳。")
        source = self.feedback(candidate["feedback_id"])
        identity = candidate.get("source", candidate)
        if any(source[key] != identity[key] for key in ("skill_name", "skill_path", "base_sha256")):
            raise ValueError("候选和来源反馈的目标版本不匹配。")
        if candidate.get("action") == "add":
            from .skill_decision import adopt_add
            return adopt_add(self, candidate)
        target = self._target(candidate)
        marker = f"<!-- personal-agent-candidate:{candidate_id} -->"
        snapshot = self.root / ".personal" / "skill-history" / candidate["skill_name"] / f"{candidate_id}.md"
        if marker.encode("utf-8") in target.file_path.read_bytes():
            # 活动文件已替换但回执落盘失败时，重试只补回执，不能再追加经验。
            if snapshot.resolve() != snapshot or not snapshot.is_file() or _sha256(snapshot.read_bytes()) != candidate["base_sha256"]:
                raise ValueError("发现采纳标记但缺少对应原版快照，请检查记录。")
        else:
            lesson = f"{marker} 来源反馈：{source['id']}。{candidate['lesson']}"
            _, snapshot = skills.evolve_project_skill(candidate["skill_name"], lesson, expected_sha256=candidate["base_sha256"], revision_id=candidate_id,
                                                     preserve_layout=candidate["version"] == 2)
        candidate.update(status="adopted", adopted_at=_now(), snapshot=snapshot.relative_to(self.root).as_posix())
        try:
            self._save(candidate)
        except (OSError, ValueError):
            raise RuntimeError(f"技能已更新并保存原版快照，但采纳回执保存失败；请重试 /skill-adopt {candidate_id} 补全回执，不会重复追加。") from None
        return candidate

    def reject(self, candidate_id: str) -> dict:
        candidate = self.candidate(candidate_id)
        if candidate["status"] == "adopted":
            raise ValueError("候选已经采纳，拒绝命令不会回滚活动技能。")
        if candidate["status"] != "rejected":
            candidate.update(status="rejected", rejected_at=_now())
            self._save(candidate)
        return candidate
