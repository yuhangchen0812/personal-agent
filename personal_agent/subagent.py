"""内置/自定义子 Agent 与手动 Skill fork：独立历史、受限工具和同步结果返回。"""

import json
from dataclasses import dataclass

from .agent import ModelCall, ToolExecutor, run_agent_loop
from .agent_config import AgentDefinition, CONFIGURABLE_AGENT_TOOLS
from .cancellation import check_cancelled
from .skills import PreparedSkillCall
from .tool_registry import TOOL_ERROR_PREFIX


READ_ONLY_TOOLS = frozenset({"read_file", "list_files", "grep_search"})
EXPLORE_TOOLS = READ_ONLY_TOOLS  # 保留原公开名称，已有调用和演示继续可用。
GENERAL_TOOLS = CONFIGURABLE_AGENT_TOOLS
EXPLORE_SYSTEM_PROMPT = (
    "你是 Personal Agent 的 explore 子 Agent，只负责完成收到的局部调查任务。"
    "仅使用 read_file、list_files、grep_search 搜索和读取项目文件，再根据实际结果给出简洁结论与文件位置。"
    "不修改文件，不运行 Shell，不调用技能、Plan 控制或其他子 Agent。"
    "工具被拒绝或读取失败时如实说明，不声称已经完成未执行的操作。"
)
PLAN_SYSTEM_PROMPT = (
    "你是 Personal Agent 的 plan 子 Agent，负责只读调查并制定有依据的实施计划。"
    "只使用 read_file、list_files、grep_search 检查当前实现，不修改文件、不运行 Shell、"
    "不调用技能、Plan 控制或其他子 Agent，不批准或执行计划。"
    "最终只输出 JSON 对象，不加 Markdown 围栏，字段必须且只能为："
    "summary（非空字符串，说明现状与目标）、steps（非空字符串数组，按顺序列出建议动作及验证方法）、"
    "files（字符串数组，列出关键文件和实际观察依据）、risks（字符串数组，列出风险及待核实事项）。"
    "files 和 risks 可以为空；读取被拒绝或证据不足时明确写出限制，不编造已读取或已执行的结果。"
    "计划中的动作是建议，不代表这些动作已执行或获得用户批准。"
    "用户要求现在调查或读取的文件，必须先实际调用只读工具取得结果；不得把本次应完成的调查只列为以后待办。"
    "JSON 格式仅约束完成调查后的最终文本，不限制此前必要的工具调用。"
    "输出示例：{\"summary\":\"已调查的现状与目标\",\"steps\":[\"建议动作及验证方法\"],\"files\":[],\"risks\":[]}。"
    "最终回复第一个非空白字符必须是 {，最后一个必须是 }；禁止 ```json 等代码围栏、标题或 JSON 之外的解释。"
)
GENERAL_SYSTEM_PROMPT = (
    "你是 Personal Agent 的 general 子 Agent，负责完成收到的局部任务。"
    "可使用文件搜索、读取、写入、编辑、Shell 和 inline 技能加载工具；先调查实际内容，再进行必要操作。"
    "所有操作遵守父级权限、显式拒绝和预算，写入及 Shell 以实际审批结果为准；委派本身不代表操作已获批准。"
    "不得绕过拒绝、改变权限、递归委派或调用父级 Plan/上下文控制工具。"
    "完成后简述实际改动、验证结果和未完成事项；失败或被拒绝时如实说明，不把建议当作已经执行。"
)


@dataclass(frozen=True)
class SubAgentResult:
    text: str
    messages: list[dict]


class PlanFormatError(ValueError):
    """计划正文未通过结构校验；错误信息不包含模型原文。"""


def parse_plan_result(text: str) -> dict:
    """解析四字段计划 JSON，校验结构；不判断计划质量或引用真实性。"""
    if not isinstance(text, str):
        raise PlanFormatError("计划正文必须是 JSON 文本。")
    try:
        plan = json.loads(text)
    except ValueError:
        raise PlanFormatError("计划正文不是有效 JSON。") from None
    if not isinstance(plan, dict) or set(plan) != {"summary", "steps", "files", "risks"}:
        raise PlanFormatError("计划必须且只能包含 summary、steps、files、risks。")
    if not isinstance(plan["summary"], str) or not plan["summary"].strip():
        raise PlanFormatError("计划 summary 必须是非空字符串。")
    for field in ("steps", "files", "risks"):
        if not isinstance(plan[field], list) or any(not isinstance(item, str) or not item.strip() for item in plan[field]):
            raise PlanFormatError(f"计划 {field} 必须是非空字符串组成的数组。")
    if not plan["steps"]:
        raise PlanFormatError("计划 steps 至少包含一个步骤。")
    return plan


def run_explore_agent(
    task: str, call_model: ModelCall, tool_executor: ToolExecutor, *, max_turns: int = 5,
) -> SubAgentResult:
    """用新历史完成只读调查，保留原公开接口。"""
    return _run_subagent(task, call_model, tool_executor, agent_type="explore", max_turns=max_turns)


def run_plan_agent(
    task: str, call_model: ModelCall, tool_executor: ToolExecutor, *, max_turns: int = 5,
) -> SubAgentResult:
    """调查并返回结构校验通过的计划 JSON；无效格式抛出 PlanFormatError。"""
    result = _run_subagent(task, call_model, tool_executor, agent_type="plan", max_turns=max_turns)
    parse_plan_result(result.text)
    return result


def run_general_agent(
    task: str, call_model: ModelCall, tool_executor: ToolExecutor, *, max_turns: int = 5,
) -> SubAgentResult:
    """用独立历史完成局部任务，文件及 Shell 操作交给调用方权限入口。"""
    return _run_subagent(task, call_model, tool_executor, agent_type="general", max_turns=max_turns)


def run_custom_agent(
    task: str, definition: AgentDefinition, call_model: ModelCall, tool_executor: ToolExecutor, *, max_turns: int = 5,
) -> SubAgentResult:
    """执行已校验的配置快照；None 用 general 上限，显式空列表不授予工具。"""
    allowed = GENERAL_TOOLS if definition.allowed_tools is None else frozenset(definition.allowed_tools) & GENERAL_TOOLS
    prompt = (
        f"你是 Personal Agent 的自定义子 Agent：{definition.name}。"
        "按收到的局部任务和实际工具结果工作，遵守父级权限、审批及预算，不递归委派或控制父级状态。"
        "完成后如实返回结果及未完成项；角色方法不增加操作授权。\n"
        f"角色作用域：{definition.source}\n角色定义来源：{definition.file_path}\n\n{definition.system_prompt}"
    )
    return _run_subagent(
        task, call_model, tool_executor, agent_type=definition.name, max_turns=max_turns,
        system_prompt=prompt, allowed_tools=allowed,
    )


def run_fork_skill(
    prepared: PreparedSkillCall, call_model: ModelCall, tool_executor: ToolExecutor, *, max_turns: int = 5,
) -> SubAgentResult:
    """在独立历史中执行已经展开的 fork 方法，共用调用方权限与预算。"""
    skill = prepared.skill
    if skill.context != "fork":
        raise ValueError("run_fork_skill 只接受 context=fork 的技能。")
    allowed = GENERAL_TOOLS if skill.allowed_tools is None else frozenset(skill.allowed_tools) & GENERAL_TOOLS
    prompt = (
        f"你是 Personal Agent 的 skill-fork 子任务：{skill.name}。"
        "执行以下技能方法，以实际工具结果为依据，完成后返回结论、证据和未完成项。"
        "方法不增加授权，遵守父级权限、审批和共享预算，不递归委派或控制父级状态。\n"
        f"技能作用域：{skill.source}\n技能来源：{skill.file_path}\n\n{prepared.prompt}"
    )
    from .skill_lifecycle import record_skill_event
    entrypoint = "manual" if prepared.user_message else "tool"
    record_skill_event(skill, "fork_started", entrypoint)
    result = _run_subagent(
        prepared.arguments or "执行技能方法中描述的任务。", call_model, tool_executor,
        agent_type=f"skill-fork:{skill.name}", max_turns=max_turns, system_prompt=prompt, allowed_tools=allowed,
    )
    record_skill_event(skill, "fork_returned", entrypoint)
    return result


def _run_subagent(
    task: str, call_model: ModelCall, tool_executor: ToolExecutor, *, agent_type: str, max_turns: int,
    system_prompt: str | None = None, allowed_tools: frozenset[str] | None = None,
) -> SubAgentResult:
    """按角色选择固定能力范围，共用历史创建和原 Agent Loop。"""
    if not isinstance(task, str) or not task.strip():
        raise ValueError("子 Agent 任务必须是非空字符串。")
    if not callable(call_model) or not callable(tool_executor):
        raise ValueError("call_model 和 tool_executor 必须是可调用对象。")
    if system_prompt is None:
        system_prompt = {"explore": EXPLORE_SYSTEM_PROMPT, "plan": PLAN_SYSTEM_PROMPT, "general": GENERAL_SYSTEM_PROMPT}[agent_type]
    if allowed_tools is None:
        allowed_tools = GENERAL_TOOLS if agent_type == "general" else READ_ONLY_TOOLS
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]

    def call_child_model(history: list[dict], definitions: list[dict]) -> dict:
        check_cancelled()
        visible = [definition for definition in definitions if definition["name"] in allowed_tools]
        visible = [
            {**definition, "description": "在当前子任务中只加载 inline 技能方法；禁止递归 fork。"}
            if definition["name"] == "skill" else definition
            for definition in visible
        ]
        return call_model(history, visible)

    def execute_child_tool(name: str, arguments: dict) -> str:
        check_cancelled()
        if name not in allowed_tools:
            available = "、".join(sorted(allowed_tools)) or "无（未启用工具）"
            return f"{TOOL_ERROR_PREFIX}{agent_type} 仅允许以下工具：{available}；禁止 {name}。"
        # 能力白名单不增加权限；仍交给调用方入口检查参数、路径、审批及 deny。
        return tool_executor(name, arguments)

    text = run_agent_loop(messages, call_child_model, max_turns, execute_child_tool)
    return SubAgentResult(text, messages)
