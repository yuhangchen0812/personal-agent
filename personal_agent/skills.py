"""用户/项目技能发现、单文件加载、参数替换与 inline 消息准备。"""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .cancellation import check_cancelled
from .frontmatter import parse_frontmatter
from .skill_cache import current_skill_cache, reset_skill_cache


PROJECT_ROOT = Path(__file__).resolve().parent.parent
USER_SKILLS_ROOT = Path.home() / ".personal" / "skills"
SKILL_FIELDS = {"name", "description", "when-to-use", "when_to_use", "allowed-tools", "user-invocable", "context"}
BUILTIN_COMMAND_NAMES = {"mcp", "agents", "plan", "skill", "skills", "skill-create", "skill-evolve", "skill-stats", "skill-archive", "skill-archives", "skill-restore", "skill-learn", "skill-jobs", "skill-decisions", "feedback", "skill-feedback", "extract_now", "skill-draft", "skill-candidates", "skill-adopt", "skill-reject", "skill-eval", "skill-eval-judge", "skill-eval-tools", "skill-evals", "skill-champions", "memory", "session-memory", "compact", "clear", "usage", "cost"}
SKILL_ERROR_PREFIX = "技能调用失败："
BUILTIN_COMMAND_NAMES.add("skill-uses")


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    description: str
    prompt_template: str
    file_path: Path
    when_to_use: str | None = None
    allowed_tools: tuple[str, ...] | None = None
    user_invocable: bool = True
    context: str = "inline"
    source: str = "project"
    source_sha256: str | None = None

    @property
    def skill_dir(self) -> Path:
        return self.file_path.parent


@dataclass(frozen=True)
class PreparedSkillCall:
    """手动调用快照；prompt 供执行，user_message 供父会话保存。"""
    skill: SkillDefinition
    arguments: str
    prompt: str
    user_message: str


def _skill_path(file_path: str | Path) -> tuple[Path, str]:
    supplied = Path(file_path).expanduser()
    if supplied.name != "SKILL.md":
        raise ValueError("技能文件名必须为 SKILL.md。")
    root = PROJECT_ROOT.resolve()
    path = (root / supplied).resolve()
    if path.name != "SKILL.md":
        raise ValueError("技能文件不能链接到其他文件名。")
    user_root = USER_SKILLS_ROOT.absolute()
    if path.is_relative_to(user_root):
        if user_root.resolve() != user_root:
            raise ValueError("用户技能目录不能链接到其他位置。")
        relative = path.relative_to(user_root)
        if len(relative.parts) != 2:
            raise ValueError("用户技能只允许 ~/.personal/skills/名称/SKILL.md。")
        source = "user"
    elif path.is_relative_to(root):
        relative = path.relative_to(root)
        source = "project"
    else:
        raise ValueError("技能文件须在本项目或 ~/.personal/skills/名称/SKILL.md 范围内。")
    parts = [part.rstrip(" .").casefold() for part in relative.parts]
    if any(part in {"reference", ".git", ".venv", "__pycache__"} or part == ".env" or part.startswith(".env.") for part in parts):
        raise ValueError("不能从参考源码、配置或环境目录加载技能。")
    if source == "project" and ".personal" in parts and not (len(parts) == 4 and parts[:2] == [".personal", "skills"]):
        raise ValueError("运行目录只允许 .personal/skills/名称/SKILL.md。")
    return path, source


def _allowed_tools(value: str | None) -> tuple[str, ...] | None:
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
        raise ValueError("allowed-tools 必须是 JSON 字符串数组或逗号分隔的非空名称。")
    return tuple(dict.fromkeys(tool.strip() for tool in tools))


def _validate_skill_name(name: str) -> None:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_\-\u4e00-\u9fff]{1,64}", name):
        raise ValueError("技能名称须为 1～64 个中英文字、数字、下划线或短横线。")


def load_skill(file_path: str | Path) -> SkillDefinition:
    """从项目路径或用户技能路径加载独立定义，不扫描目录。"""
    path, source = _skill_path(file_path)
    try:
        source_bytes = path.read_bytes()
        raw = source_bytes.decode("utf-8-sig")
    except FileNotFoundError:
        raise ValueError(f"找不到技能文件：{path}。") from None
    except UnicodeError:
        raise ValueError("SKILL.md 必须使用 UTF-8 编码。") from None
    parsed = parse_frontmatter(raw)
    meta = parsed.meta
    unknown = meta.keys() - SKILL_FIELDS
    if unknown:
        raise ValueError(f"暂不支持技能字段：{', '.join(sorted(unknown))}。")
    if "when-to-use" in meta and "when_to_use" in meta:
        raise ValueError("when-to-use 和 when_to_use 只能填写一个。")
    name = meta.get("name") or path.parent.name
    _validate_skill_name(name)
    if not parsed.body.strip():
        raise ValueError("技能正文不能为空。")
    invocable = meta.get("user-invocable", "true").casefold()
    if invocable not in {"true", "false"}:
        raise ValueError("user-invocable 必须为 true 或 false。")
    context = meta.get("context", "inline")
    if context not in {"inline", "fork"}:
        raise ValueError("context 必须为 inline 或 fork。")
    return SkillDefinition(
        name=name, description=meta.get("description", ""), prompt_template=parsed.body, file_path=path,
        when_to_use=meta.get("when-to-use", meta.get("when_to_use")) or None,
        allowed_tools=_allowed_tools(meta.get("allowed-tools")), user_invocable=invocable == "true", context=context,
        source=source, source_sha256=hashlib.sha256(source_bytes).hexdigest(),
    )


def discover_project_skills() -> tuple[list[SkillDefinition], list[str]]:
    """扫描项目技能目录的一层子目录，返回无歧义的定义和跳过原因。"""
    return _discover_directory(PROJECT_ROOT.resolve() / ".personal" / "skills")


def _discovered_skill(path: Path) -> SkillDefinition:
    """缓存只用于发现；路径限制及文件状态每次检查，直接 load_skill 始终读盘。"""
    cache = current_skill_cache()
    if cache is None:
        return load_skill(path)
    key = (PROJECT_ROOT.resolve(), USER_SKILLS_ROOT.absolute(), path.absolute())

    def signature():
        resolved, source = _skill_path(path)
        state = resolved.stat()
        return (resolved, source, state.st_dev, state.st_ino, state.st_size, state.st_mtime_ns, state.st_ctime_ns)

    return cache.definition(key, signature, lambda: load_skill(path))


def _scan_skill_directory(directory: Path) -> tuple[list[SkillDefinition], list[str]]:
    """读取一层目录中的有效定义，保留重名项供发现或创建检查。"""
    if directory.resolve() != directory:
        raise ValueError(f"技能目录不能链接到其他位置：{directory}。")
    if not directory.exists():
        return [], []
    if not directory.is_dir():
        raise ValueError(f"技能根路径必须是目录：{directory}。")
    skills = []
    warnings = []
    for entry in sorted(directory.iterdir(), key=lambda path: (path.name.casefold(), path.name)):
        path = entry / "SKILL.md"
        try:
            if not entry.is_dir() or not path.exists():
                continue
            skill = _discovered_skill(path)
        except (ValueError, OSError) as error:
            warnings.append(f"跳过 {path}：{error}")
            continue
        skills.append(skill)
    return skills, warnings


def _discover_directory(directory: Path) -> tuple[list[SkillDefinition], list[str]]:
    """发现时停用同作用域重名项，不影响其他名称。"""
    definitions, warnings = _scan_skill_directory(directory)
    by_name: dict[str, list[SkillDefinition]] = {}
    for skill in definitions:
        by_name.setdefault(skill.name, []).append(skill)
    skills = []
    for name, definitions in sorted(by_name.items()):
        if len(definitions) > 1:
            sources = "、".join(str(skill.file_path) for skill in definitions)
            warnings.append(f"技能名重复：{name}，已停用该作用域同名项，请修改 name：{sources}")
        else:
            skills.append(definitions[0])
    return skills, warnings


def discover_skills() -> tuple[list[SkillDefinition], list[str]]:
    """先用户级再项目级，按 name 保留首个有效且无歧义的定义。"""
    selected: dict[str, SkillDefinition] = {}
    warnings = []
    for directory in (USER_SKILLS_ROOT.absolute(), PROJECT_ROOT.resolve() / ".personal" / "skills"):
        try:
            skills, skipped = _discover_directory(directory)
        except (ValueError, OSError) as error:
            warnings.append(f"跳过技能目录：{error}")
            continue
        warnings.extend(skipped)
        for skill in skills:
            selected.setdefault(skill.name, skill)
    return sorted(selected.values(), key=lambda skill: skill.name), warnings


def resolve_skill_prompt(skill: SkillDefinition, arguments: str = "") -> str:
    """一次性替换模板占位符；参数中的反斜杠和占位符保持字面文本。"""
    values = {
        "$ARGUMENTS": arguments,
        "${ARGUMENTS}": arguments,
        "${CLAUDE_SKILL_DIR}": str(skill.skill_dir),
    }
    return re.sub(
        r"\$ARGUMENTS(?![A-Za-z0-9_])|\$\{ARGUMENTS\}|\$\{CLAUDE_SKILL_DIR\}",
        lambda match: values[match.group()], skill.prompt_template,
    )


def prepare_inline_skill(command: str) -> str:
    """从 /skill 路径 参数 加载一个技能，返回可保存的 user 消息正文。"""
    return _inline_skill_message(prepare_skill_call(command))


def prepare_named_skill(command: str, skills: list[SkillDefinition]) -> str:
    """按元信息 name 精确查找，使用合并后选中的定义准备 inline 消息。"""
    return _inline_skill_message(prepare_skill_call(command, skills))


def prepare_skill_call(command: str, skills: list[SkillDefinition] | None = None) -> PreparedSkillCall:
    """解析两种手动调用，固定配置及替换结果；fork 的父消息不包含方法正文。"""
    parts = command.split(maxsplit=1)
    if not parts or not parts[0].startswith("/"):
        raise ValueError("技能调用须使用 /技能名 参数 或 /skill 路径 参数。")
    if parts[0].lower() == "/skill":
        match = re.fullmatch(r'/skill\s+(?:"([^"\r\n]+)"|([^"\s]+))(?:\s+(.*))?', command, re.IGNORECASE)
        if match is None:
            raise ValueError('用法：/skill 项目内/SKILL.md [参数]；路径含空格时用双引号包住。')
        skill = load_skill(match.group(1) or match.group(2))
        arguments = match.group(3) or ""
    else:
        name = parts[0][1:]
        if name.casefold() in BUILTIN_COMMAND_NAMES:
            raise ValueError(f"/{name} 是内置命令；同名技能请用 /skill 路径 参数 调用。")
        skill = next((skill for skill in (skills or []) if skill.name == name), None)
        if skill is None:
            raise ValueError(f"未知技能：{name}；输入 /skills 查看可用名称。")
        skill = _fresh_named_skill(skill, name)
        arguments = parts[1] if len(parts) == 2 else ""
    if not skill.user_invocable:
        raise ValueError(f"技能 {skill.name} 的 user-invocable 为 false，不允许手动调用。")
    prompt = resolve_skill_prompt(skill, arguments)
    location = str(skill.file_path) if skill.source == "user" else skill.file_path.relative_to(PROJECT_ROOT.resolve()).as_posix()
    header = f"技能调用：{command}\n技能名称：{skill.name}\n技能作用域：{skill.source}\n技能来源：{location}"
    body = "技能执行方式：fork；方法在独立子任务中执行，下条助手消息为子任务结论。" if skill.context == "fork" else prompt
    return PreparedSkillCall(skill, arguments, prompt, f"{header}\n\n{body}")


def _inline_skill_message(prepared: PreparedSkillCall) -> str:
    """保留原 inline 函数的返回契约，禁止将 fork 当成展开正文。"""
    if prepared.skill.context != "inline":
        raise ValueError(f"技能 {prepared.skill.name} 使用 fork，请通过工具聊天的手动入口执行。")
    return prepared.user_message


def _fresh_named_skill(selected: SkillDefinition, name: str) -> SkillDefinition:
    """实际调用重读正文及权限元信息，名称/来源改变时拒绝旧名称调用。"""
    fresh = load_skill(selected.file_path)
    if fresh.name != name or fresh.source != selected.source:
        raise ValueError("技能名称或作用域在调用前已变化，请重新发现后再调用。")
    return fresh


def _find_tool_skill(skill_name: str) -> SkillDefinition:
    """模型入口共用名称发现；允许内部技能，不执行手动调用许可检查。"""
    check_cancelled()
    skills, warnings = discover_skills()
    check_cancelled()
    skill = next((skill for skill in skills if skill.name == skill_name), None)
    if skill is None:
        detail = "；" + "；".join(warnings) if warnings else ""
        raise ValueError(f"未知技能：{skill_name}{detail}。")
    return _fresh_named_skill(skill, skill_name)


def _format_inline_skill(skill: SkillDefinition, prompt: str) -> str:
    return (
        f"已加载 inline 技能：{skill.name}\n技能作用域：{skill.source}\n技能来源：{skill.file_path}\n"
        "以下是技能方法，按当前用户任务判断是否采用。加载正文不代表任务已完成，"
        "也不批准写入、Shell 或 Plan 执行；继续遵守当前工具权限。\n\n" + prompt
    )


def load_inline_skill(skill_name: str, args: str = "") -> str:
    """保留只加载 inline 的入口；不提供运行时回调或递归 fork。"""
    try:
        skill = _find_tool_skill(skill_name)
        if skill.context != "inline":
            raise ValueError(f"技能 {skill.name} 使用 fork，此入口仅支持 inline。")
        prompt = resolve_skill_prompt(skill, args)
        check_cancelled()
        from .skill_lifecycle import record_skill_event
        record_skill_event(skill, "inline_loaded", "inline_api")
        return _format_inline_skill(skill, prompt)
    except (ValueError, OSError) as error:
        return f"{SKILL_ERROR_PREFIX}{error}"


def load_skill_for_tool(
    skill_name: str, args: str = "", *, fork_skill: Callable[[PreparedSkillCall], str] | None = None,
) -> str:
    """父运行时可执行 fork；无回调时仅加载 inline，配置快照只读取一次。"""
    try:
        skill = _find_tool_skill(skill_name)
        prompt = resolve_skill_prompt(skill, args)
        check_cancelled()
    except (ValueError, OSError) as error:
        return f"{SKILL_ERROR_PREFIX}{error}"
    if skill.context == "inline":
        from .skill_lifecycle import record_skill_event
        record_skill_event(skill, "inline_loaded", "tool")
        return _format_inline_skill(skill, prompt)
    if fork_skill is None:
        return f"{SKILL_ERROR_PREFIX}技能 {skill.name} 使用 fork，需要父聊天运行时；子任务中禁止递归 fork。"
    prepared = PreparedSkillCall(skill, args, prompt, "")
    result = fork_skill(prepared)
    return f"fork 技能已返回：{skill.name}\n技能作用域：{skill.source}\n技能来源：{skill.file_path}\n\n{result}"


def check_project_skill_creation(name: str) -> Path:
    """只检查新建名称、目录及冲突，不创建文件；返回固定项目目标。"""
    _validate_skill_name(name)
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)}
    if name.casefold() in BUILTIN_COMMAND_NAMES or name.upper() in reserved:
        raise ValueError("技能名称不能与内置命令或 Windows 保留设备名相同。")
    directory = PROJECT_ROOT.resolve() / ".personal" / "skills"
    path = directory / name / "SKILL.md"
    resolved, source = _skill_path(path)
    if resolved != path or source != "project":
        raise ValueError("创建目标必须是本项目技能目录中的原路径，不能通过链接写入其他位置。")
    for scope in (USER_SKILLS_ROOT.absolute(), directory):
        definitions, _ = _scan_skill_directory(scope)
        if any(skill.name == name for skill in definitions):
            raise ValueError(f"技能名称已存在：{name}，请选择新名称。")
    if path.parent.exists():
        raise ValueError(f"目标技能目录已存在：{path.parent}，未覆盖。")
    return path


def create_project_skill(name: str, description: str, instructions: str, when_to_use: str = "") -> Path:
    """显式创建一个项目 inline 技能，拒绝同名定义和已有目标目录。"""
    path = check_project_skill_creation(name)
    directory = path.parent.parent
    if not isinstance(description, str) or not description.strip():
        raise ValueError("技能描述必须是非空字符串。")
    if not isinstance(instructions, str) or not instructions.strip():
        raise ValueError("技能正文必须是非空字符串。")
    if not isinstance(when_to_use, str):
        raise ValueError("适用场景必须是字符串，可为空。")

    metadata = {"name": name, "description": description, "user-invocable": "true", "context": "inline"}
    if when_to_use:
        metadata["when-to-use"] = when_to_use
    header = "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in metadata.items())
    body = instructions.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    document = f"---\n{header}\n---\n{body}\n"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.mkdir()  # 独占新目录，已有目录（即使为空）也不接管。
    except FileExistsError:
        raise ValueError(f"目标技能目录已存在：{path.parent}，未覆盖。") from None
    created_file = False
    try:
        if _skill_path(path) != (path, "project"):
            raise ValueError("创建目标路径已变化，已停止写入。")
        with path.open("x", encoding="utf-8", newline="\n") as target:
            created_file = True
            target.write(document)
    except BaseException:
        # 只清理本次创建的文件与目录，不递归删除或处理已有目录。
        if created_file:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            path.parent.rmdir()
        except OSError:
            pass
        raise
    finally:
        reset_skill_cache()
    return path


def get_project_skill(name: str) -> SkillDefinition:
    """取得当前有效的项目维护目标，拒绝用户覆盖和项目技能目录外的定义。"""
    _validate_skill_name(name)
    skills, warnings = discover_skills()
    skill = next((skill for skill in skills if skill.name == name), None)
    if skill is None:
        detail = "；" + "；".join(warnings) if warnings else ""
        raise ValueError(f"找不到可演化的技能：{name}{detail}。")
    if skill.source != "project":
        raise ValueError(f"当前选中的 {name} 是用户技能；本命令只演化项目技能，请先处理同名覆盖。")

    root = PROJECT_ROOT.resolve()
    directory = root / ".personal" / "skills"
    path = skill.file_path
    if directory.resolve() != directory or path.parent.parent != directory or _skill_path(path) != (path, "project"):
        raise ValueError("演化目标必须是本项目 .personal/skills 中的原路径。")
    return skill


def evolve_project_skill(
    name: str, lesson: str, *, expected_sha256: str | None = None, revision_id: str | None = None,
    preserve_layout: bool = False,
) -> tuple[Path, Path]:
    """追加经验并返回技能/原版快照路径；候选可指定版本和可重试的快照名称。"""
    if not isinstance(lesson, str) or not lesson.strip():
        raise ValueError("演化经验必须是非空字符串。")
    if revision_id is not None and (not isinstance(revision_id, str) or not re.fullmatch(r"cand_[a-f0-9]{32}", revision_id)):
        raise ValueError("候选快照 ID 无效。")
    lesson = lesson.replace("\r\n", "\n").replace("\r", "\n").strip() if preserve_layout else re.sub(r"\s+", " ", lesson).strip()
    skill = get_project_skill(name)
    path, root = skill.file_path, PROJECT_ROOT.resolve()
    history = root / ".personal" / "skill-history" / name
    if history.resolve() != history:
        raise ValueError("技能历史目录不能通过链接写入其他位置。")

    original = path.read_bytes()
    if expected_sha256 is not None and hashlib.sha256(original).hexdigest() != expected_sha256:
        raise ValueError("技能版本已变化，请重新记录反馈和生成候选。")
    now = datetime.now(timezone.utc)
    revision = revision_id or f"{now:%Y%m%dT%H%M%S%fZ}-{uuid4().hex}"
    snapshot = history / f"{revision}.md"
    newline = b"\r\n" if b"\r\n" in original else b"\n"
    note = f"## 手动演化记录（{now.isoformat()}，UTC）\n\n{lesson}\n"
    # 原文件逐字节保留为前缀；新增段落沿用常见的 CRLF/LF 换行。
    updated = original + newline * 2 + note.encode("utf-8").replace(b"\n", newline)

    history.mkdir(parents=True, exist_ok=True)
    snapshot_created = False
    try:
        if revision_id is not None and snapshot.exists():
            if snapshot.is_symlink() or snapshot.read_bytes() != original:
                raise ValueError("已有候选快照与原版本不一致，未修改技能。")
        else:
            with snapshot.open("xb") as target:
                snapshot_created = True
                target.write(original)
    except BaseException:
        if snapshot_created:
            try:
                snapshot.unlink(missing_ok=True)
            except OSError:
                pass
        raise

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".evolve-", suffix=".tmp", delete=False) as target:
            temporary_path = Path(target.name)
            target.write(updated)
        # 避免发现技能后发生的常见外部编辑被覆盖；这里不提供跨进程锁。
        if _skill_path(path) != (path, "project") or path.read_bytes() != original:
            raise ValueError("技能文件在演化期间发生变化，已停止替换；原始快照保留。")
        os.replace(temporary_path, path)
        reset_skill_cache()
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
    return path, snapshot


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="解析项目或用户技能 SKILL.md，输出定义，不调用模型或执行技能。")
    parser.add_argument("file_path", type=Path, help="项目内或 ~/.personal/skills/名称/SKILL.md 的路径")
    args = parser.parse_args(argv)
    try:
        skill = load_skill(args.file_path)
    except (ValueError, OSError) as error:
        print(f"技能加载失败：{error}", file=sys.stderr)
        raise SystemExit(1) from None
    result = asdict(skill)
    result["file_path"] = str(skill.file_path)
    result["skill_dir"] = str(skill.skill_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
