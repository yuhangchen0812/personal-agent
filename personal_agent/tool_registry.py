"""工具注册、参数校验和经过权限判断的统一执行入口。"""

import argparse
import json
import sys
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path

from .agent_config import AgentDefinition, BUILTIN_AGENT_TYPES, CONFIGURABLE_AGENT_TOOLS
from .mcp_runtime import McpToolRuntime
from .permissions import ApprovalCallback, PERMISSION_MODES, SUPPORTED_TOOLS, check_permission, confirm_in_terminal
from .plan import PlanSession
from .skills import PreparedSkillCall, SKILL_ERROR_PREFIX, load_skill_for_tool
from .tools import (
    EDIT_ERROR_PREFIX,
    ERROR_PREFIX,
    LIST_ERROR_PREFIX,
    SEARCH_ERROR_PREFIX,
    SHELL_ERROR_PREFIX,
    WRITE_ERROR_PREFIX,
    edit_file,
    grep_search,
    list_files,
    read_file,
    run_shell,
    write_file,
)


TOOL_ERROR_PREFIX = "工具调用失败："
TOOL_REGISTRY = {
    "read_file": {
        "description": "读取项目内 UTF-8 文本或生成的完整工具结果；默认 offset/limit 按行读取，提供 char_offset 时改按字符读取。",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "minLength": 1, "description": "项目内文件路径"},
                "offset": {"type": "integer", "minimum": 1, "default": 1, "description": "起始行号，从 1 开始"},
                "limit": {"type": "integer", "minimum": 1, "default": 200, "description": "最多返回的行数"},
                "char_offset": {"type": "integer", "minimum": 1, "description": "可选字符起点，从 1 开始；提供后忽略行 offset/limit"},
                "char_limit": {"type": "integer", "minimum": 1, "description": "字符模式最多返回字符数，默认 4000；建议不超过 4000"},
            },
            "required": ["file_path"],
            "additionalProperties": False,
        },
        "handler": read_file,
    },
    "list_files": {
        "description": "按 glob 列出项目内文件；支持 *.py、**/*.py，不支持花括号组合。",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "minLength": 1, "description": "文件路径 glob 模式"},
                "path": {"type": "string", "minLength": 1, "default": ".", "description": "搜索起始目录"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        "handler": list_files,
    },
    "grep_search": {
        "description": "按正则搜索项目内 UTF-8 文件，返回项目内路径、行号和匹配原文。",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "minLength": 1, "description": "正文正则表达式"},
                "path": {"type": "string", "minLength": 1, "default": ".", "description": "搜索目录或单个文件"},
                "include": {"type": ["string", "null"], "minLength": 1, "default": None, "description": "可选的文件名或相对路径 glob，不支持花括号组合"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        "handler": grep_search,
    },
    "write_file": {
        "description": "在 playground 内创建或整体覆盖 UTF-8 文件，返回行号预览。",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "minLength": 1, "description": "playground 内的项目相对文件路径"},
                "content": {"type": "string", "description": "文件完整新内容，允许空字符串"},
            },
            "required": ["file_path", "content"],
            "additionalProperties": False,
        },
        "handler": write_file,
    },
    "edit_file": {
        "description": "精确替换 playground 文件中唯一匹配的文本，返回 diff。",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "minLength": 1, "description": "playground 内的现有文件路径"},
                "old_string": {"type": "string", "minLength": 1, "description": "必须唯一匹配的精确旧文本"},
                "new_string": {"type": "string", "description": "替换文本，空字符串表示删除"},
            },
            "required": ["file_path", "old_string", "new_string"],
            "additionalProperties": False,
        },
        "handler": edit_file,
    },
    "run_shell": {
        "description": "从 playground 内启动 PowerShell，返回 stdout、stderr、退出码或超时结果。",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "minLength": 1, "description": "PowerShell 命令"},
                "cwd": {"type": "string", "minLength": 1, "default": "playground", "description": "playground 内的项目相对启动目录"},
                "timeout": {"type": "integer", "minimum": 1, "default": 30, "description": "等待时限，单位秒"},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        "handler": run_shell,
    },
    "enter_plan_mode": {
        "description": "进入 Plan 规划状态，返回唯一计划文件路径；规划期间仅可修改该计划文件，禁止执行任务写入和 Shell。",
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "handler": None,
    },
    "exit_plan_mode": {
        "description": "提交已写入计划文件的实际正文，等待用户确认；只有批准后才能恢复原权限模式并执行任务。无参数，不能自行传入批准标记。",
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "handler": None,
    },
    "compact_context": {
        "description": "登记上下文折叠请求，本轮任务成功结束后才整理旧轮次，当前任务完整保留。具名会话保存独立记忆，无具名聊天生成摘要。不要重复登记；停用、预算或校验失败时不折叠。记忆不是操作授权。",
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "handler": None,
    },
    "skill": {
        "description": "按名称使用匹配当前任务的技能：inline 返回方法供后续使用，fork 在独立子任务中执行并返回结论。可使用内部技能；各操作仍受父级权限、审批和共享预算约束，子任务不能递归 fork。",
        "input_schema": {
            "type": "object",
            "properties": {
                "skill_name": {"type": "string", "minLength": 1, "description": "已发现的技能 name，精确匹配，不是文件路径"},
                "args": {"type": "string", "default": "", "description": "传给技能模板的任务参数，作为字面文本替换"},
            },
            "required": ["skill_name"],
            "additionalProperties": False,
        },
        "handler": load_skill_for_tool,
    },
    "tool_search": {
        "description": "按名称或中英文关键词检索本次已配置的外部 MCP 工具，并激活最多 5 个匹配项。默认延迟工具不会预先提供参数 Schema；检索命中后，下一次模型请求才会提供完整定义，请随后再按返回的精确名称调用。检索不会执行外部工具，不增加权限；找不到时换关键词，不猜测工具名。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "description": "工具名称或描述关键词，例如 echo、回显、天气"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3, "description": "最多激活几个匹配工具，1～5"},
            },
            "required": ["query"], "additionalProperties": False,
        },
        "handler": None,
    },
    "agent": {
        "description": "委派局部任务：explore 只读调查，plan 只读返回四字段 JSON 计划，general 可读写/编辑文件、运行 Shell 和加载技能。各操作仍遵守父级权限、审批和共享预算，委派不额外授权；内部历史不加入当前对话，不递归委派或控制父 Plan/上下文。",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "minLength": 1, "description": "完整的局部调查目标及必要背景，子任务不会自动获得当前对话历史"},
                "type": {"type": "string", "enum": list(BUILTIN_AGENT_TYPES), "default": "explore", "description": "内置角色或本轮可用自定义配置的精确 name；所有操作仍受父级权限约束"},
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
        "handler": None,
    },
}


def get_tool_definitions() -> list[dict]:
    """返回可序列化的工具说明副本，不包含实际 Python 函数。"""
    return [
        {
            "name": name,
            "description": tool["description"],
            "input_schema": deepcopy(tool["input_schema"]),
        }
        for name, tool in TOOL_REGISTRY.items()
    ]


def with_custom_agent_definitions(definitions: list[dict], agents: dict[str, AgentDefinition]) -> list[dict]:
    """只在父请求副本中扩展 agent 类型和候选元信息，不暴露角色正文。"""
    if not agents:
        return definitions
    copied = deepcopy(definitions)
    for tool in copied:
        if tool["name"] != "agent":
            continue
        types = tool["input_schema"]["properties"]["type"]["enum"]
        tool["input_schema"]["properties"]["type"]["enum"] = list(dict.fromkeys([*types, *agents]))
        metadata = [
            {"name": definition.name, "description": definition.description,
             "source": definition.source,
             "tools": sorted(CONFIGURABLE_AGENT_TOOLS) if definition.allowed_tools is None else list(definition.allowed_tools)}
            for definition in agents.values()
        ]
        tool["description"] += "\n本轮自定义角色元信息（项目优先），仅供选择，不代表操作授权：" + json.dumps(metadata, ensure_ascii=False)
    return copied


def validate_tool_arguments(name: str, arguments: dict, *, agent_types: list[str] | None = None) -> dict:
    """按当前工具使用的 Schema 子集校验，返回补齐默认值的新字典。"""
    if not isinstance(name, str) or name not in TOOL_REGISTRY:
        raise ValueError(f"未知工具：{name!r}。")
    if not isinstance(arguments, dict):
        raise ValueError("工具参数必须是字典（JSON object）。")

    schema = TOOL_REGISTRY[name]["input_schema"]
    if name == "agent" and agent_types:
        schema = deepcopy(schema)
        schema["properties"]["type"]["enum"] = list(dict.fromkeys([*BUILTIN_AGENT_TYPES, *agent_types]))
    properties = schema["properties"]
    missing = [key for key in schema["required"] if key not in arguments]
    if missing:
        raise ValueError(f"缺少必填参数：{', '.join(missing)}。")
    unknown = arguments.keys() - properties.keys()
    if unknown:
        raise ValueError(f"不支持的参数：{', '.join(sorted(map(str, unknown)))}。")

    validated = {}
    for key, rule in properties.items():
        if key in arguments:
            value = arguments[key]
        elif "default" in rule:
            value = rule["default"]
        else:
            continue

        types = rule["type"] if isinstance(rule["type"], list) else [rule["type"]]
        valid_type = (
            ("string" in types and isinstance(value, str))
            or ("integer" in types and isinstance(value, int) and not isinstance(value, bool))
            or ("null" in types and value is None)
        )
        if not valid_type:
            raise ValueError(f"参数 {key} 的类型必须是 {'/'.join(types)}。")
        if "enum" in rule and value not in rule["enum"]:
            raise ValueError(f"参数 {key} 必须是 {'/'.join(rule['enum'])}。")
        if "minimum" in rule and value < rule["minimum"]:
            raise ValueError(f"参数 {key} 必须大于等于 {rule['minimum']}。")
        if "maximum" in rule and value > rule["maximum"]:
            raise ValueError(f"参数 {key} 必须小于等于 {rule['maximum']}。")
        if isinstance(value, str) and len(value) < rule.get("minLength", 0):
            raise ValueError(f"参数 {key} 不能为空字符串。")
        validated[key] = value
    return validated


def execute_tool(
    name: str,
    arguments: dict,
    *,
    mode: str = "default",
    allow: list[str] | None = None,
    deny: list[str] | None = None,
    confirm: ApprovalCallback | None = None,
    plan: PlanSession | None = None,
    request_compaction: Callable[[], str] | None = None,
    delegate: Callable[[str, str], str] | None = None,
    agent_types: list[str] | None = None,
    fork_skill: Callable[[PreparedSkillCall], str] | None = None,
    mcp: McpToolRuntime | None = None,
    search_tools: Callable[[str, int], str] | None = None,
    exposed_mcp_tools: set[str] | None = None,
) -> str:
    """校验与权限检查通过后才执行；需要确认但未获批准时不调用工具。"""
    external_names = mcp.names if mcp is not None else set()
    try:
        if isinstance(name, str) and name in external_names:
            if not isinstance(arguments, dict):
                raise ValueError("MCP 工具参数必须是字典（JSON object）。")
            # 外部 Schema 原样交给模型，完整实参约束由 Server 校验；不套用内置 Schema 子集。
            validated = deepcopy(arguments)
        else:
            validated = validate_tool_arguments(name, arguments, agent_types=agent_types)
    except ValueError as error:
        return f"{TOOL_ERROR_PREFIX}{error}"
    if plan is not None:
        mode = plan.mode
    plan_file_edit = plan is not None and plan.is_plan_file_edit(name, validated)
    decision = check_permission(name, mode, allow=allow, deny=deny, plan_file_edit=plan_file_edit, external_tools=external_names)
    if decision["action"] == "deny":
        return f"{TOOL_ERROR_PREFIX}权限拒绝：{decision['message']}"
    if name in external_names and exposed_mcp_tools is not None and name not in exposed_mcp_tools:
        return f"{TOOL_ERROR_PREFIX}本次模型请求尚未提供该 MCP 工具定义；先 tool_search 检索，在下一次请求取得 Schema 后再调用。"
    if name == "tool_search":
        if search_tools is None:
            return f"{TOOL_ERROR_PREFIX}本次没有可检索的 MCP 运行时。"
        try:
            return search_tools(validated["query"], validated["limit"])
        except ValueError as error:
            return f"{TOOL_ERROR_PREFIX}{error}"
    if name in {"enter_plan_mode", "exit_plan_mode"}:
        if plan is None:
            return f"{TOOL_ERROR_PREFIX}Plan 控制工具需要同一聊天的状态对象。"
        return plan.enter() if name == "enter_plan_mode" else plan.submit(confirm)
    if name == "compact_context":
        if request_compaction is None:
            return f"{TOOL_ERROR_PREFIX}compact_context 需要聊天运行时登记回调。"
        return request_compaction()
    if name == "agent":
        if delegate is None:
            return f"{TOOL_ERROR_PREFIX}agent 需要聊天运行时委派回调。"
        return delegate(validated["prompt"], validated["type"])
    if decision["action"] == "confirm":
        if confirm is None:
            return f"{TOOL_ERROR_PREFIX}需要确认但未提供审批回调，{name} 未执行。"
        if confirm(name, validated, decision["message"]) is not True:
            return f"{TOOL_ERROR_PREFIX}本次操作未获批准，{name} 未执行。"
    if name in external_names:
        return mcp.call(name, validated)
    handler = TOOL_REGISTRY[name]["handler"]
    if name == "skill":
        return handler(**validated, fork_skill=fork_skill)
    return handler(**validated)


def main() -> None:
    """从 UTF-8 JSON 文件读取参数，演示一次统一工具调用。"""
    parser = argparse.ArgumentParser(description="按工具名和 JSON 参数文件调用工具，并检查权限。")
    parser.add_argument("name", choices=sorted(TOOL_REGISTRY), help="工具名")
    parser.add_argument("arguments_file", type=Path, help="保存参数对象的 UTF-8 JSON 文件")
    parser.add_argument("--permission-mode", choices=PERMISSION_MODES, default="default")
    parser.add_argument("--allow-tool", choices=sorted(SUPPORTED_TOOLS), action="append")
    parser.add_argument("--deny-tool", choices=sorted(SUPPORTED_TOOLS), action="append")
    args = parser.parse_args()
    try:
        arguments = json.loads(args.arguments_file.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as error:
        print(f"{TOOL_ERROR_PREFIX}无法读取参数 JSON（{type(error).__name__}）。", file=sys.stderr)
        raise SystemExit(1) from None

    result = execute_tool(args.name, arguments, mode=args.permission_mode, allow=args.allow_tool, deny=args.deny_tool, confirm=confirm_in_terminal)
    if result.startswith((TOOL_ERROR_PREFIX, SKILL_ERROR_PREFIX, ERROR_PREFIX, LIST_ERROR_PREFIX, SEARCH_ERROR_PREFIX, WRITE_ERROR_PREFIX, EDIT_ERROR_PREFIX, SHELL_ERROR_PREFIX, "命令执行失败。", "命令超时")):
        print(result, file=sys.stderr)
        raise SystemExit(1)
    print(result)


if __name__ == "__main__":
    main()
