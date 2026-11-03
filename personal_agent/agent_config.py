"""项目/用户自定义 Agent 的加载、目录发现和项目优先合并，不启动子任务。"""

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from .frontmatter import parse_frontmatter
from .permissions import EDIT_TOOLS, READ_TOOLS


PROJECT_ROOT = Path(__file__).resolve().parent.parent
USER_AGENTS_ROOT = Path.home() / ".personal" / "agents"
AGENT_FIELDS = {"name", "description", "allowed-tools"}
CONFIGURABLE_AGENT_TOOLS = frozenset(READ_TOOLS | EDIT_TOOLS | {"run_shell", "skill"})
BUILTIN_AGENT_TYPES = ("explore", "plan", "general")


@dataclass(frozen=True)
class AgentDefinition:
    name: str
    description: str
    system_prompt: str
    allowed_tools: tuple[str, ...] | None
    file_path: Path
    source: str = "project"


def _agent_definition_path(file_path: str | Path) -> tuple[Path, str]:
    """返回真实路径与来源；项目外仅开放用户 agents 目录的一层 Markdown。"""
    root = PROJECT_ROOT.resolve()
    supplied = Path(file_path).expanduser()
    path = (root / supplied).resolve()
    if supplied.suffix.casefold() != ".md" or path.suffix.casefold() != ".md":
        raise ValueError("Agent 配置必须是 .md 文件。")
    user_root = USER_AGENTS_ROOT.absolute()
    if path.is_relative_to(user_root):
        if user_root.resolve() != user_root:
            raise ValueError("用户 Agent 目录不能链接到其他位置。")
        relative = path.relative_to(user_root)
        if len(relative.parts) != 1:
            raise ValueError("用户 Agent 只允许 ~/.personal/agents/名称.md。")
        source = "user"
    elif path.is_relative_to(root):
        relative = path.relative_to(root)
        source = "project"
    else:
        raise ValueError("Agent 配置必须位于当前项目或 ~/.personal/agents/名称.md。")
    parts = [part.rstrip(" .").casefold() for part in relative.parts]
    blocked = {"reference", ".git", ".venv", "__pycache__", ".idea"}
    if any(part in blocked or part == ".env" or part.startswith(".env.") for part in parts):
        raise ValueError("不能从参考源码、版本库、环境或私有配置目录加载 Agent。")
    if source == "project" and ".personal" in parts and not (len(parts) == 3 and parts[:2] == [".personal", "agents"]):
        raise ValueError("运行目录只允许 .personal/agents/名称.md。")
    return path, source


def _parse_allowed_tools(value: str | None) -> tuple[str, ...] | None:
    """保留缺省与空工具列表的区别，拒绝未知或父级控制工具。"""
    if value is None:
        return None
    if value.startswith("["):
        try:
            tools = json.loads(value)
        except ValueError:
            raise ValueError("allowed-tools 数组必须是有效 JSON。") from None
    else:
        tools = value.split(",")
    if not isinstance(tools, list) or any(not isinstance(tool, str) or not tool.strip() for tool in tools):
        raise ValueError("allowed-tools 必须是 JSON 字符串数组或逗号分隔的非空工具名；无工具请写 []。")
    names = tuple(dict.fromkeys(tool.strip() for tool in tools))
    unknown = set(names) - CONFIGURABLE_AGENT_TOOLS
    if unknown:
        raise ValueError(f"Agent 暂不支持这些工具：{'、'.join(sorted(unknown))}。")
    return names


def load_agent_definition(file_path: str | Path) -> AgentDefinition:
    """读取单个 UTF-8 Markdown 配置并返回定义；不执行提示词或申请工具权限。"""
    path, source = _agent_definition_path(file_path)
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise ValueError(f"找不到 Agent 配置：{path}。") from None
    except UnicodeError:
        raise ValueError("Agent 配置必须使用 UTF-8 编码。") from None
    parsed = parse_frontmatter(raw)
    unknown = parsed.meta.keys() - AGENT_FIELDS
    if unknown:
        raise ValueError(f"暂不支持 Agent 字段：{', '.join(sorted(unknown))}。")
    name = parsed.meta.get("name") or path.stem
    if not re.fullmatch(r"[A-Za-z0-9_\-\u4e00-\u9fff]{1,64}", name):
        raise ValueError("Agent 名称须为 1～64 个中英文字、数字、下划线或短横线。")
    if not parsed.body.strip():
        raise ValueError("Agent 系统提示词不能为空。")
    return AgentDefinition(
        name=name,
        description=parsed.meta.get("description", ""),
        system_prompt=parsed.body,
        allowed_tools=_parse_allowed_tools(parsed.meta.get("allowed-tools")),
        file_path=path,
        source=source,
    )


def discover_project_agents() -> tuple[list[AgentDefinition], list[str]]:
    """扫描项目 agents 目录的一层 Markdown，跳过坏文件并停用同名有效项。"""
    return _discover_agent_directory(PROJECT_ROOT.resolve() / ".personal" / "agents")


def _discover_agent_directory(directory: Path) -> tuple[list[AgentDefinition], list[str]]:
    """共用单层扫描与同作用域重名处理；根目录错误向上传递。"""
    if directory.resolve() != directory:
        raise ValueError(f"Agent 目录不能链接到其他位置：{directory}。")
    if not directory.exists():
        return [], []
    if not directory.is_dir():
        raise ValueError(f"Agent 根路径必须是目录：{directory}。")
    by_name: dict[str, list[AgentDefinition]] = {}
    warnings = []
    for entry in sorted(directory.iterdir(), key=lambda path: (path.name.casefold(), path.name)):
        try:
            if entry.suffix.casefold() != ".md" or not entry.is_file():
                continue
            if entry.resolve() != entry:
                raise ValueError("发现入口必须是该目录中的原文件，不能通过链接加载其他位置。")
            definition = load_agent_definition(entry)
        except (ValueError, OSError, RuntimeError) as error:
            warnings.append(f"跳过 {entry}：{error}")
            continue
        by_name.setdefault(definition.name, []).append(definition)
    definitions = []
    for name, matches in sorted(by_name.items(), key=lambda item: (item[0].casefold(), item[0])):
        if len(matches) > 1:
            locations = "、".join(str(definition.file_path) for definition in matches)
            warnings.append(f"Agent 名重复：{name}，已停用同名项，请修改 name：{locations}")
        else:
            definitions.append(matches[0])
    return definitions, warnings


def discover_agents() -> tuple[list[AgentDefinition], list[str]]:
    """项目有效定义优先于用户定义，坏目录不影响另一作用域。"""
    selected: dict[str, AgentDefinition] = {}
    warnings = []
    for directory in (PROJECT_ROOT.resolve() / ".personal" / "agents", USER_AGENTS_ROOT.absolute()):
        try:
            definitions, skipped = _discover_agent_directory(directory)
        except (ValueError, OSError, RuntimeError) as error:
            warnings.append(f"跳过 Agent 目录：{error}")
            continue
        warnings.extend(skipped)
        for definition in definitions:
            selected.setdefault(definition.name, definition)
    return sorted(selected.values(), key=lambda definition: (definition.name.casefold(), definition.name)), warnings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="读取单个项目或用户 Agent Markdown 配置，输出 JSON，不调用模型或执行任务。")
    parser.add_argument("file_path", type=Path, help="项目内 Markdown 或 ~/.personal/agents/名称.md")
    args = parser.parse_args(argv)
    try:
        definition = load_agent_definition(args.file_path)
    except (ValueError, OSError) as error:
        print(f"Agent 配置加载失败：{error}", file=sys.stderr)
        raise SystemExit(1) from None
    result = asdict(definition)
    result["file_path"] = str(definition.file_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
