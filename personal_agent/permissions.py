"""权限判断与终端确认；判断函数本身不执行工具。"""

import json
from collections.abc import Callable


PERMISSION_MODES = ("default", "plan", "acceptEdits", "dontAsk", "bypassPermissions")
READ_TOOLS = {"read_file", "list_files", "grep_search"}
EDIT_TOOLS = {"write_file", "edit_file"}
PLAN_TOOLS = {"enter_plan_mode", "exit_plan_mode"}
CONTEXT_TOOLS = {"compact_context"}
DELEGATION_TOOLS = {"agent", "skill"}
DISCOVERY_TOOLS = {"tool_search"}
SUPPORTED_TOOLS = READ_TOOLS | EDIT_TOOLS | {"run_shell"} | PLAN_TOOLS | CONTEXT_TOOLS | DELEGATION_TOOLS | DISCOVERY_TOOLS
ApprovalCallback = Callable[[str, dict, str], bool]


def check_permission(
    tool_name: str,
    mode: str = "default",
    allow: list[str] | None = None,
    deny: list[str] | None = None,
    *,
    plan_file_edit: bool = False,
    external_tools: set[str] | None = None,
) -> dict[str, str]:
    """返回 action=allow/deny/confirm 及原因；规则仅精确匹配工具名。"""
    if not isinstance(mode, str) or mode not in PERMISSION_MODES:
        return {"action": "deny", "message": f"未知权限模式：{mode!r}。"}
    supported = SUPPORTED_TOOLS | (external_tools or set())
    if not isinstance(tool_name, str) or tool_name not in supported:
        return {"action": "deny", "message": f"未知工具：{tool_name!r}。"}
    for label, rules in (("allow", allow), ("deny", deny)):
        if rules is not None and (
            not isinstance(rules, list)
            or any(not isinstance(rule, str) or rule not in supported for rule in rules)
        ):
            return {"action": "deny", "message": f"{label} 必须是已知工具名列表。"}

    if tool_name in (deny or []):
        return {"action": "deny", "message": f"{tool_name} 被显式 deny 规则禁止。"}
    if tool_name in DISCOVERY_TOOLS:
        return {"action": "allow", "message": "只检索并加载已发现的工具定义；不执行外部工具或批准后续操作。"}
    if tool_name in PLAN_TOOLS:
        return {"action": "allow", "message": "Plan 控制工具；退出规划仍需独立审批。"}
    if tool_name in CONTEXT_TOOLS:
        return {"action": "allow", "message": "上下文控制工具；仅登记历史折叠，不批准任务操作。"}
    if tool_name in DELEGATION_TOOLS:
        return {"action": "allow", "message": "允许加载方法或启动子任务；具体操作仍受子工具范围及父级权限、审批和拒绝规则约束。"}
    if mode == "plan" and tool_name not in READ_TOOLS:
        if plan_file_edit and tool_name in EDIT_TOOLS:
            return {"action": "allow", "message": "规划状态允许修改本次指定的计划文件。"}
        return {"action": "deny", "message": f"plan 模式仅允许只读工具，禁止 {tool_name}。"}
    if mode == "bypassPermissions":
        return {"action": "allow", "message": "bypassPermissions 模式跳过询问，显式 deny 仍生效。"}
    if tool_name in (allow or []):
        return {"action": "allow", "message": f"{tool_name} 被显式 allow 规则允许。"}
    if tool_name in READ_TOOLS:
        return {"action": "allow", "message": f"{tool_name} 是只读工具。"}
    if mode == "acceptEdits" and tool_name in EDIT_TOOLS:
        return {"action": "allow", "message": f"acceptEdits 模式自动允许 {tool_name}。"}
    if mode == "dontAsk":
        return {"action": "deny", "message": f"{tool_name} 需要确认，dontAsk 模式自动拒绝。"}
    return {"action": "confirm", "message": f"{tool_name} 需要用户确认。"}


def confirm_in_terminal(tool_name: str, arguments: dict, message: str) -> bool:
    """显示本次完整参数，只有 y/yes 才批准；空输入、EOF 或中断均拒绝。"""
    print(f"\n等待确认：{message}")
    print(f"工具：{tool_name}")
    print(json.dumps(arguments, ensure_ascii=False, indent=2))
    try:
        return input("允许本次操作？[y/N] ").strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        print("\n审批已取消，本次工具未执行。")
        return False
