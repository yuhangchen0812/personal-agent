"""一次聊天中的 Plan 状态；计划文件仍放在 playground 内。"""

import re
from uuid import uuid4

from . import tools
from .permissions import ApprovalCallback, EDIT_TOOLS, PERMISSION_MODES
from .tools import _resolve_write_path


def validate_plan_snapshot(snapshot: dict) -> dict:
    """校验可持久化状态，不接受权限模式或任意文件路径。"""
    if not isinstance(snapshot, dict) or set(snapshot) != {"state", "plan_file", "content"}:
        raise ValueError("Plan 存档结构不正确。")
    state, file_path, content = snapshot["state"], snapshot["plan_file"], snapshot["content"]
    if state not in ("executing", "planning", "awaiting_approval"):
        raise ValueError("未知 Plan 存档状态。")
    if state == "executing":
        if file_path is not None or content is not None:
            raise ValueError("执行状态不应保留未批准的计划文件。")
    else:
        if not isinstance(file_path, str) or not re.fullmatch(r"playground/\.plans/plan-[0-9a-f]{32}\.md", file_path):
            raise ValueError("Plan 存档只能使用本项目生成的计划路径。")
        if _resolve_write_path(file_path) != tools.PROJECT_ROOT / file_path:
            raise ValueError("计划文件的实际路径已改变。")
        if content is not None and not isinstance(content, str):
            raise ValueError("Plan 正文必须是字符串或 null。")
        if state == "awaiting_approval" and (content is None or not content.strip()):
            raise ValueError("等待批准的计划必须包含正文。")
    return dict(snapshot)


class PlanSession:
    """保存执行/规划/等待批准状态；状态和计划路径由应用维护。"""

    def __init__(self, mode: str = "default") -> None:
        if mode not in PERMISSION_MODES:
            raise ValueError(f"未知权限模式：{mode!r}。")
        self.mode = "default" if mode == "plan" else mode
        self.state = "executing"
        self.plan_file: str | None = None
        self._plan_path = None
        self._resume_mode = self.mode
        if mode == "plan":
            self.enter()

    def snapshot(self) -> dict:
        """返回状态、计划路径和正文；不保存 mode、旧权限或批准凭据。"""
        if self.state == "executing":
            return {"state": "executing", "plan_file": None, "content": None}
        path = _resolve_write_path(self.plan_file)
        if path != self._plan_path:
            raise ValueError("计划文件的实际路径已改变。")
        try:
            content = path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            content = None
        return validate_plan_snapshot({"state": self.state, "plan_file": self.plan_file, "content": content})

    def restore(self, snapshot: dict) -> None:
        """恢复未批准状态；批准后的执行权限取本次创建对象时的模式。"""
        data = validate_plan_snapshot(snapshot)
        if data["state"] == "executing":
            return  # 本次显式选择 plan 时，保留新进入的规划状态。
        path = _resolve_write_path(data["plan_file"])
        try:
            current = path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            current = None
        if current is not None and current != data["content"]:
            raise ValueError("计划文件与存档正文不同，已停止恢复以保留现有内容。")
        if current is None and data["content"] is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8", newline="\n") as target:
                target.write(data["content"])
        self._resume_mode = self._resume_mode if self.mode == "plan" else self.mode
        self.mode = "plan"
        self.state = data["state"]
        self.plan_file = data["plan_file"]
        self._plan_path = path

    def describe(self) -> str:
        """返回可显示、可发送给模型的当前状态说明。"""
        result = f"Plan 状态：{self.state}；当前权限模式：{self.mode}。"
        if self.plan_file:
            result += f"\n计划文件：{self.plan_file}"
        if self.state != "executing":
            result += "\n先用只读工具调查；仅规划状态可写入/编辑这个计划文件。完成后调用 exit_plan_mode 提交实际正文，等待用户确认。未批准时不得执行任务。"
        if self.state == "awaiting_approval":
            result += "\n尚未获得批准；再次调用 exit_plan_mode 重新展示计划并询问。"
        return result

    def enter(self) -> str:
        """进入规划，记住原权限模式，并分配本次唯一计划路径。"""
        if self.state != "executing":
            return self.describe()
        plan_file = f"playground/.plans/plan-{uuid4().hex}.md"
        plan_path = _resolve_write_path(plan_file)
        self._resume_mode = self.mode
        self.plan_file = plan_file
        self._plan_path = plan_path
        self.mode = "plan"
        self.state = "planning"
        return self.describe()

    def is_plan_file_edit(self, name: str, arguments: dict) -> bool:
        """只给规划状态中解析后精确相同的计划路径提供编辑例外。"""
        if self.state != "planning" or name not in EDIT_TOOLS:
            return False
        try:
            return _resolve_write_path(arguments["file_path"]) == self._plan_path
        except (ValueError, OSError, RuntimeError):
            return False

    def submit(self, confirm: ApprovalCallback | None = None) -> str:
        """读取实际计划并同步等待确认；只有明确 True 才恢复原模式。"""
        if self.state == "executing":
            return "工具调用失败：尚未进入 Plan，不能提交计划。"
        try:
            path = _resolve_write_path(self.plan_file)
            if path != self._plan_path:
                return "工具调用失败：计划文件的实际路径已改变，请重新检查。"
            content = path.read_text(encoding="utf-8-sig")
        except (ValueError, OSError, RuntimeError) as error:
            return f"工具调用失败：无法读取计划文件（{type(error).__name__}），请先写入计划。"
        if not content.strip():
            return "工具调用失败：计划正文为空，请先完成规划。"

        self.state = "awaiting_approval"
        if confirm is None:
            return "工具调用失败：需要计划审批但未提供回调，保持等待批准，任务未执行。"
        try:
            approved = confirm(
                "exit_plan_mode",
                {"plan_file": self.plan_file, "plan": content, "execution_mode": self._resume_mode},
                "请审阅完整计划。批准后恢复原权限模式；具体操作仍按该模式检查。",
            ) is True
        finally:
            # 回调拒绝、取消或异常时都不能停留在可执行状态。
            self.state = "planning"
        if not approved:
            return "计划未获批准，已返回规划状态；可以修改计划后再次提交。"

        self.mode = self._resume_mode
        self.state = "executing"
        return f"计划已批准。{self.describe()}\n具体工具操作仍受当前模式及 allow/deny 约束。\n\n批准的计划正文：\n{content}"
