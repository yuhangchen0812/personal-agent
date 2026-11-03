"""项目内文本/工具会话的 JSON 保存与恢复。"""

import json
import os
import re
import tempfile
from copy import deepcopy
from pathlib import Path

from .plan import validate_plan_snapshot
from .model_content import validate_anthropic_state
from .session_memory import validate_reference

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SESSION_VERSION = 1
TOOL_SESSION_VERSION = 2
PLAN_SESSION_VERSION = 3
PROVIDER_SESSION_VERSION = 4
MEMORY_SESSION_VERSION = 5
_UNSET = object()


def validate_text_messages(messages: list[dict]) -> list[dict]:
    """检查完整、交替的 user/assistant 文本轮次，返回独立的消息副本。"""
    if not isinstance(messages, list) or len(messages) % 2:
        raise ValueError("文本会话必须包含完整的 user/assistant 轮次。")
    result = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or not {"role", "content"} <= set(message) or not set(message) <= {"role", "content", "anthropic", "qwen"}:
            raise ValueError("文本会话不支持工具调用或其他消息字段。")
        role = "user" if index % 2 == 0 else "assistant"
        if message["role"] != role:
            raise ValueError("文本会话必须从 user 开始，按 user/assistant 交替排列。")
        if not isinstance(message["content"], str) or not message["content"].strip():
            raise ValueError("文本会话的 content 必须是非空字符串。")
        validate_anthropic_state(message)
        result.append(deepcopy(message))
    return result


def validate_agent_messages(messages: list[dict]) -> list[dict]:
    """检查完整任务轮次及工具调用/结果配对，返回嵌套数据的独立副本。"""
    if not isinstance(messages, list):
        raise ValueError("会话消息必须是列表。")
    expected = "user"
    pending = set()
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != expected:
            raise ValueError(f"会话消息顺序错误，当前应为 {expected}。")
        if expected == "user":
            if set(message) != {"role", "content"} or not isinstance(message["content"], str) or not message["content"].strip():
                raise ValueError("user 消息必须包含非空文本正文。")
            expected = "assistant"
        elif expected == "assistant":
            if not set(message) <= {"role", "content", "tool_calls", "anthropic", "qwen"}:
                raise ValueError("assistant 消息包含不支持的字段。")
            content = message.get("content")
            if content is not None and not isinstance(content, str):
                raise ValueError("assistant 正文必须是字符串或 null。")
            calls = message.get("tool_calls", [])
            if not isinstance(calls, list):
                raise ValueError("tool_calls 必须是列表。")
            validate_anthropic_state(message)
            if calls:
                pending = set()
                for call in calls:
                    if not isinstance(call, dict) or set(call) != {"id", "type", "function"}:
                        raise ValueError("工具请求必须包含 id/type/function。")
                    call_id = call["id"]
                    function = call["function"]
                    if not isinstance(call_id, str) or not call_id.strip() or call_id in pending:
                        raise ValueError("同批工具调用 ID 必须非空且不重复。")
                    if call["type"] != "function" or not isinstance(function, dict) or set(function) != {"name", "arguments"}:
                        raise ValueError("仅支持 function 工具请求。")
                    if not isinstance(function["name"], str) or not function["name"].strip() or not isinstance(function["arguments"], str):
                        raise ValueError("工具名必须非空，arguments 必须保留为 JSON 字符串。")
                    pending.add(call_id)
                expected = "tool"
            else:
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("最终 assistant 消息必须包含非空正文。")
                expected = "user"
        else:
            if set(message) != {"role", "tool_call_id", "content"} or not isinstance(message["content"], str):
                raise ValueError("工具结果必须包含 tool_call_id 和字符串正文。")
            call_id = message["tool_call_id"]
            if not isinstance(call_id, str) or call_id not in pending:
                raise ValueError("工具结果 ID 没有对应请求，或结果重复。")
            pending.remove(call_id)
            if not pending:
                expected = "assistant"
    if expected != "user":
        raise ValueError("会话尚未完成：缺少工具结果或最终回答。")
    return deepcopy(messages)


class Session:
    """一个项目内具名存档；可保存 Plan 状态，不保存权限或审批。"""

    def __init__(self, name: str, *, with_tools: bool = False) -> None:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
            raise ValueError("会话名须为 1～64 位字母、数字、下划线或短横线，并以字母或数字开头。")
        reserved = {"CON", "PRN", "AUX", "NUL"} | {f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10)}
        if name.upper() in reserved:
            raise ValueError("会话名不能使用 Windows 保留设备名。")
        self.name = name
        self.with_tools = with_tools
        self.plan_snapshot: dict | None = None
        self.session_memory: dict | None = None
        self.project_root = PROJECT_ROOT.resolve()
        directory = self.project_root / ".personal" / "sessions"
        self.path = (directory / f"{name}.json").resolve()
        if self.path.parent != directory:
            raise ValueError("会话文件必须位于本项目的 .personal/sessions 目录。")

    def _document(self, messages: list[dict], plan: dict | None = None, *, session_memory=_UNSET) -> dict:
        """将消息与版本、名称、项目归属组合成可序列化对象。"""
        extended = any(isinstance(message, dict) and ({"anthropic", "qwen"} & set(message)) for message in messages)
        reference = validate_reference(self.session_memory if session_memory is _UNSET else session_memory)
        document = {
            "version": MEMORY_SESSION_VERSION if reference is not None else (PROVIDER_SESSION_VERSION if extended else (PLAN_SESSION_VERSION if self.with_tools else SESSION_VERSION)),
            "name": self.name,
            "project_root": str(self.project_root),
            "messages": validate_agent_messages(messages) if self.with_tools else validate_text_messages(messages),
        }
        if extended or reference is not None:
            document["with_tools"] = self.with_tools
        if reference is not None:
            document["session_memory"] = reference
        if self.with_tools:
            snapshot = self.plan_snapshot if plan is None else plan
            document["plan"] = validate_plan_snapshot(snapshot) if snapshot is not None else None
        elif plan is not None:
            raise ValueError("普通文本会话不能保存 Plan 状态。")
        elif extended or reference is not None:
            document["plan"] = None
        return document

    def create(self) -> list[dict]:
        """独占创建空存档并返回空历史；同名文件已存在时不覆盖。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open("x", encoding="utf-8", newline="\n") as target:
                json.dump(self._document([]), target, ensure_ascii=False, indent=2)
                target.write("\n")
        except FileExistsError:
            raise ValueError(f"会话 {self.name} 已存在，请使用 --resume {self.name}。") from None
        return []

    def load(self) -> list[dict]:
        """读取并校验指定存档；不自动选择最新文件或其他项目的会话。"""
        try:
            document = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            raise ValueError(f"找不到会话 {self.name}，请先用 --session 创建。") from None
        except (UnicodeError, json.JSONDecodeError):
            raise ValueError("会话文件不是有效的 UTF-8 JSON，原文件未修改。") from None
        fields = {"version", "name", "project_root", "messages"}
        if not isinstance(document, dict):
            raise ValueError("会话文件结构不正确。")
        version = document.get("version")
        if type(version) is not int or version not in (SESSION_VERSION, TOOL_SESSION_VERSION, PLAN_SESSION_VERSION, PROVIDER_SESSION_VERSION, MEMORY_SESSION_VERSION):
            raise ValueError("不支持这个会话文件版本。")
        if version in (PLAN_SESSION_VERSION, PROVIDER_SESSION_VERSION, MEMORY_SESSION_VERSION):
            fields.add("plan")
        if version in (PROVIDER_SESSION_VERSION, MEMORY_SESSION_VERSION):
            fields.add("with_tools")
        if version == MEMORY_SESSION_VERSION:
            fields.add("session_memory")
        if set(document) != fields:
            raise ValueError("会话文件结构不正确。")
        if document["name"] != self.name:
            raise ValueError("会话文件中的名称与请求的名称不一致。")
        project = document["project_root"]
        if not isinstance(project, str) or not Path(project).is_absolute() or Path(project).resolve() != self.project_root:
            raise ValueError("会话属于其他项目，不能在当前项目恢复。")
        with_tools = version != SESSION_VERSION
        if version in (PROVIDER_SESSION_VERSION, MEMORY_SESSION_VERSION):
            if type(document["with_tools"]) is not bool:
                raise ValueError("扩展会话的 with_tools 须为布尔值。")
            with_tools = document["with_tools"]
            if not with_tools and document["plan"] is not None:
                raise ValueError("普通文本会话不能保存 Plan 状态。")
        messages = validate_agent_messages(document["messages"]) if with_tools else validate_text_messages(document["messages"])
        if version not in (PROVIDER_SESSION_VERSION, MEMORY_SESSION_VERSION) and any({"anthropic", "qwen"} & set(message) for message in messages):
            raise ValueError("原始协议内容须使用 version=4/5 会话。")
        snapshot = document.get("plan")
        reference = validate_reference(document.get("session_memory"))
        if version == MEMORY_SESSION_VERSION and reference is None:
            raise ValueError("version=5 会话必须包含 session-memory 引用。")
        self.plan_snapshot = validate_plan_snapshot(snapshot) if snapshot is not None else None
        self.with_tools = with_tools
        self.session_memory = reference
        return messages

    def save(self, messages: list[dict], *, plan: dict | None = None, session_memory=_UNSET) -> None:
        """先验证，再写同目录临时文件并替换存档；失败时清理临时文件。"""
        document = self._document(messages, plan, session_memory=session_memory)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", dir=self.path.parent,
                prefix=f".{self.name}-", suffix=".tmp", delete=False,
            ) as target:
                temporary_path = Path(target.name)
                json.dump(document, target, ensure_ascii=False, indent=2)
                target.write("\n")
            os.replace(temporary_path, self.path)
            self.plan_snapshot = document.get("plan")
            self.session_memory = document.get("session_memory")
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


# 保留上一小步的导入名称；默认仍创建纯文本存档。
TextSession = Session
