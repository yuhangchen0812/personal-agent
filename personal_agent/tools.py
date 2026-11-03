"""Personal Agent 的文件与命令工具，可先脱离模型直接运行。"""

import argparse
import base64
import difflib
import fnmatch
import os
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

from .cancellation import check_cancelled
from .tool_output import is_tool_result_path
from .workspace import project_root


PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXCLUDED_DIRECTORIES = {"reference", ".venv", ".git", ".personal", "__pycache__"}
ERROR_PREFIX = "读取文件失败："
LIST_ERROR_PREFIX = "列出文件失败："
SEARCH_ERROR_PREFIX = "搜索文件失败："
WRITE_ERROR_PREFIX = "写入文件失败："
EDIT_ERROR_PREFIX = "编辑文件失败："
SHELL_ERROR_PREFIX = "运行命令失败："


def _resolve_read_path(file_path: str, *, allow_tool_result: bool = False) -> Path:
    """将路径转换为项目内可读取的真实路径；不合规则抛出 ValueError。"""
    if not isinstance(file_path, str) or not file_path.strip():
        raise ValueError("file_path 必须是非空字符串。")
    if "\x00" in file_path:
        raise ValueError("file_path 不能包含空字符。")

    path = Path(file_path)
    if not path.is_absolute():
        path = project_root(PROJECT_ROOT) / path
    path = path.resolve()

    try:
        relative = path.relative_to(project_root(PROJECT_ROOT))
    except ValueError:
        raise ValueError("只允许读取 Personal Agent 项目内的文件。") from None

    if any(":" in part for part in relative.parts):
        raise ValueError("不支持包含冒号的特殊文件路径。")
    parts = [part.rstrip(" .").casefold() for part in relative.parts]
    if any(part in EXCLUDED_DIRECTORIES for part in parts) and not (
        allow_tool_result and is_tool_result_path(relative)
    ):
        raise ValueError("该路径位于排除目录中，不能通过文件工具读取。")
    if any(
        (part == ".env" or part.startswith(".env.")) and part != ".env.example"
        for part in parts
    ):
        raise ValueError("不能读取 .env 或 .env.* 配置；允许读取 .env.example。")

    return path


def _read_text(path: Path) -> str:
    """按 64 Ki 字符分块读取 UTF-8 文本，在块之间响应取消。"""
    chunks = []
    with path.open("r", encoding="utf-8-sig") as source:
        while True:
            check_cancelled()
            chunk = source.read(64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    check_cancelled()
    return "".join(chunks)


def read_file(
    file_path: str, offset: int = 1, limit: int = 200,
    *, char_offset: int | None = None, char_limit: int = 4000,
) -> str:
    """默认按行读取；提供 char_offset 时改按字符分段，两种位置均从 1 开始。"""
    check_cancelled()
    positions = [("offset", offset), ("limit", limit), ("char_limit", char_limit)]
    if char_offset is not None:
        positions.append(("char_offset", char_offset))
    for name, value in positions:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            return f"{ERROR_PREFIX}{name} 必须是正整数，不能是布尔值。"

    try:
        path = _resolve_read_path(file_path, allow_tool_result=True)
        if path.is_dir():
            return f"{ERROR_PREFIX}路径指向目录，请提供文件路径。"
        text = _read_text(path)
    except UnicodeDecodeError:
        return f"{ERROR_PREFIX}文件不是有效的 UTF-8 文本，请先转换编码。"
    except ValueError as error:
        return f"{ERROR_PREFIX}{error}"
    except FileNotFoundError:
        return f"{ERROR_PREFIX}文件不存在：{file_path}"
    except PermissionError:
        return f"{ERROR_PREFIX}没有权限读取该文件。"
    except (OSError, RuntimeError) as error:
        return f"{ERROR_PREFIX}无法访问文件（{type(error).__name__}）。"

    if "\x00" in text:
        return f"{ERROR_PREFIX}文件含有空字符，可能是二进制文件。"
    if char_offset is not None:
        if not text:
            return "(空文件)"
        if char_offset > len(text):
            return f"{ERROR_PREFIX}char_offset={char_offset} 超出文件末尾（共 {len(text)} 字符）。"
        end = min(char_offset - 1 + char_limit, len(text))
        result = f"[字符 {char_offset}-{end}，共 {len(text)} 字符。]\n" + text[char_offset - 1:end]
        if end < len(text):
            result += f"\n[继续读取：char_offset={end + 1}, char_limit={char_limit}。]"
        return result
    lines = text.splitlines()
    if not lines:
        return "(空文件)"
    if offset > len(lines):
        return f"{ERROR_PREFIX}offset={offset} 超出文件末尾（共 {len(lines)} 行）。"

    end = min(offset - 1 + limit, len(lines))
    selected = lines[offset - 1:end]
    numbered = "\n".join(
        f"{number:4d} | {line}"
        for number, line in enumerate(selected, start=offset)
    )
    if offset > 1 or end < len(lines):
        note = f"显示第 {offset}-{end} 行，共 {len(lines)} 行。"
        if end < len(lines):
            note += f" 继续读取：offset={end + 1}, limit={limit}。"
        numbered += f"\n\n[{note}]"
    return numbered


def _resolve_write_path(file_path: str) -> Path:
    """复用项目路径检查，再将写入目标限制在 playground 内。"""
    path = _resolve_read_path(file_path)
    try:
        relative = path.relative_to(project_root(PROJECT_ROOT) / "playground")
    except ValueError:
        raise ValueError("只允许写入 playground 目录内的文件。") from None
    if not relative.parts or path.is_dir():
        raise ValueError("请提供 playground 内的文件路径，不能写入目录。")
    return path


def write_file(file_path: str, content: str) -> str:
    """以 UTF-8 创建或整体覆盖 playground 文件，返回结果和内容预览。"""
    if not isinstance(content, str):
        return f"{WRITE_ERROR_PREFIX}content 必须是字符串。"
    try:
        path = _resolve_write_path(file_path)
        existed = path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    except ValueError as error:
        return f"{WRITE_ERROR_PREFIX}{error}"
    except (OSError, RuntimeError) as error:
        return f"{WRITE_ERROR_PREFIX}无法写入文件（{type(error).__name__}）。"

    lines = content.splitlines()
    action = "已覆盖" if existed else "已创建"
    relative = path.relative_to(project_root(PROJECT_ROOT)).as_posix()
    result = f"{action} {relative}（{len(lines)} 行）。"
    if lines:
        preview = "\n".join(f"{number:4d} | {line}" for number, line in enumerate(lines[:20], start=1))
        result += f"\n\n{preview}"
    if len(lines) > 20:
        result += "\n[仅预览前 20 行。]"
    return result


def edit_file(file_path: str, old_string: str, new_string: str) -> str:
    """在 playground 文件内替换唯一匹配文本，并返回 unified diff。"""
    if not isinstance(old_string, str) or not old_string:
        return f"{EDIT_ERROR_PREFIX}old_string 必须是非空字符串。"
    if not isinstance(new_string, str):
        return f"{EDIT_ERROR_PREFIX}new_string 必须是字符串。"
    try:
        path = _resolve_write_path(file_path)
        with path.open("r", encoding="utf-8", newline="") as source:
            original = source.read()
        if "\x00" in original:
            return f"{EDIT_ERROR_PREFIX}文件含空字符，可能是二进制文件。"

        start = original.find(old_string)
        if start < 0:
            return f"{EDIT_ERROR_PREFIX}未找到 old_string，请先读取文件并提供精确文本。"
        if original.find(old_string, start + 1) >= 0:
            return f"{EDIT_ERROR_PREFIX}old_string 存在多处匹配，请补充上下文使其唯一。"
        relative = path.relative_to(project_root(PROJECT_ROOT)).as_posix()
        if old_string == new_string:
            return f"无需修改 {relative}：old_string 与 new_string 相同。"

        updated = original.replace(old_string, new_string, 1)
        diff_lines = difflib.unified_diff(
            original.splitlines(keepends=True), updated.splitlines(keepends=True),
            fromfile=f"a/{relative}", tofile=f"b/{relative}", lineterm="",
        )
        diff = "\n".join(line.rstrip("\r\n") for line in diff_lines)
        path.write_text(updated, encoding="utf-8", newline="")
        return f"已编辑 {relative}（唯一匹配替换）。\n\n{diff}"
    except FileNotFoundError:
        return f"{EDIT_ERROR_PREFIX}文件不存在：{file_path}"
    except UnicodeDecodeError:
        return f"{EDIT_ERROR_PREFIX}文件不是有效的 UTF-8 文本。"
    except ValueError as error:
        return f"{EDIT_ERROR_PREFIX}{error}"
    except (OSError, RuntimeError) as error:
        return f"{EDIT_ERROR_PREFIX}无法编辑文件（{type(error).__name__}）。"


def _stop_shell_process(process: subprocess.Popen, flags: int) -> tuple[bytes, bytes]:
    """终止本次 Shell 进程树并限时回收管道；失败交给调用者报告。"""
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=flags, timeout=5, check=True,
            )
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    finally:
        if process.poll() is None:
            process.kill()
    return process.communicate(timeout=5)


def run_shell(command: str, cwd: str = "playground", timeout: float = 30) -> str:
    """从 playground 内启动 PowerShell，返回输出、退出码或超时结果。"""
    if not isinstance(command, str) or not command.strip():
        return f"{SHELL_ERROR_PREFIX}command 必须是非空字符串。"
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        return f"{SHELL_ERROR_PREFIX}timeout 必须是正数，单位为秒。"
    try:
        working_dir = _resolve_read_path(cwd)
        if not working_dir.is_relative_to(project_root(PROJECT_ROOT) / "playground"):
            return f"{SHELL_ERROR_PREFIX}启动目录必须位于 playground 内。"
        if not working_dir.is_dir():
            return f"{SHELL_ERROR_PREFIX}启动目录不存在或不是目录。"
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if not powershell:
            return f"{SHELL_ERROR_PREFIX}未找到 PowerShell。"

        script = (
            "$ErrorActionPreference = 'Stop'; "
            "$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
            "$LASTEXITCODE = 0;\n& {\n" + command
            + "\n}\nif ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }"
        )
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        timed_out = False
        with subprocess.Popen(
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            cwd=working_dir, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags,
            start_new_session=sys.platform != "win32",
        ) as process:
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                stdout, stderr = _stop_shell_process(process, flags)
            except KeyboardInterrupt:
                try:
                    _stop_shell_process(process, flags)
                except (OSError, subprocess.SubprocessError) as error:
                    print(f"Shell 中断后的进程清理失败（{type(error).__name__}），请检查本次命令。", file=sys.stderr)
                raise
            return_code = process.returncode

        output = stdout.decode("utf-8", errors="replace").replace("\r\n", "\n").rstrip("\n")
        errors = stderr.decode("utf-8", errors="replace").replace("\r\n", "\n").rstrip("\n")
        if timed_out:
            status = f"命令超时（{timeout:g} 秒）。"
            code = "不可用（超时终止）"
        else:
            status = "命令执行成功。" if return_code == 0 else "命令执行失败。"
            code = str(return_code)
        relative = working_dir.relative_to(project_root(PROJECT_ROOT)).as_posix()
        return f"{status}\n工作目录：{relative}\n退出码：{code}\nstdout:\n{output or '(空)'}\nstderr:\n{errors or '(空)'}"
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        return f"{SHELL_ERROR_PREFIX}无法执行命令（{type(error).__name__}）。"


def list_files(pattern: str, path: str = ".") -> str:
    """列出目录内符合 glob 的文件，返回相对于该目录的路径字符串。"""
    check_cancelled()
    if not isinstance(pattern, str) or not pattern.strip():
        return f"{LIST_ERROR_PREFIX}pattern 必须是非空字符串。"
    if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
        return f"{LIST_ERROR_PREFIX}pattern 请使用相对匹配模式，不能包含 ..。"
    if "{" in pattern or "}" in pattern:
        return f"{LIST_ERROR_PREFIX}暂不支持花括号组合，请分别搜索每种后缀。"

    try:
        base = _resolve_read_path(path)
        if not base.is_dir():
            return f"{LIST_ERROR_PREFIX}目录不存在或路径不是目录：{path}"

        files = []
        for candidate in base.glob(pattern):
            check_cancelled()
            try:
                checked = _resolve_read_path(str(candidate))
            except ValueError:
                continue
            if checked.is_file():
                files.append(candidate.relative_to(base).as_posix())

        if not files:
            return f"未找到匹配 {pattern!r} 的文件；可尝试 **/*，无匹配不代表目录为空。"
        files.sort()
        result = "\n".join(files[:200])
        if len(files) > 200:
            result += "\n[仅显示前 200 个文件。]"
        return result
    except (ValueError, OSError, RuntimeError) as error:
        return f"{LIST_ERROR_PREFIX}{error}"


def _iter_search_files(root: Path) -> Iterator[Path]:
    """产生允许搜索的文件；遍历目录时提前跳过排除目录。"""
    check_cancelled()
    if root.is_file():
        yield root
        return

    for directory, subdirs, names in root.walk():
        check_cancelled()
        allowed = []
        for name in sorted(subdirs):
            check_cancelled()
            try:
                _resolve_read_path(str(directory / name))
            except ValueError:
                continue
            allowed.append(name)
        subdirs[:] = allowed

        for name in sorted(names):
            check_cancelled()
            candidate = directory / name
            try:
                checked = _resolve_read_path(str(candidate))
            except ValueError:
                continue
            if checked.is_file():
                yield candidate


def grep_search(pattern: str, path: str = ".", include: str | None = None) -> str:
    """在 UTF-8 文件中逐行搜索正则，返回项目内路径、行号及匹配原文。"""
    check_cancelled()
    if not isinstance(pattern, str) or not pattern:
        return f"{SEARCH_ERROR_PREFIX}pattern 必须是非空字符串。"
    if include is not None and (not isinstance(include, str) or not include.strip()):
        return f"{SEARCH_ERROR_PREFIX}include 必须是非空字符串或 None。"
    if include and ("{" in include or "}" in include):
        return f"{SEARCH_ERROR_PREFIX}include 暂不支持花括号组合，请分别搜索。"
    try:
        regex = re.compile(pattern)
    except re.error as error:
        return f"{SEARCH_ERROR_PREFIX}正则表达式无效：{error}"

    try:
        root = _resolve_read_path(path)
        if not root.exists():
            return f"{SEARCH_ERROR_PREFIX}路径不存在：{path}"
        base = root if root.is_dir() else root.parent
        matches = []
        skipped = 0
        for candidate in _iter_search_files(root):
            check_cancelled()
            if include:
                names = (candidate.name, candidate.relative_to(base).as_posix())
                patterns = (include, include[3:]) if include.startswith("**/") else (include,)
                if not any(fnmatch.fnmatch(name, glob) for name in names for glob in patterns):
                    continue
            try:
                text = _read_text(candidate)
            except (UnicodeDecodeError, OSError):
                skipped += 1
                continue
            if "\x00" in text:
                skipped += 1
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                check_cancelled()
                if regex.search(line):
                    relative = candidate.relative_to(project_root(PROJECT_ROOT)).as_posix()
                    matches.append(f"{relative}:{line_number}:{line}")
                    if len(matches) > 100:
                        break
            if len(matches) > 100:
                break

        result = "\n".join(matches[:100]) if matches else "未找到匹配内容。"
        if len(matches) > 100:
            result += "\n[仅显示前 100 条匹配。]"
        if skipped:
            result += f"\n[跳过 {skipped} 个非 UTF-8 文本或无法读取的文件。]"
        return result
    except (ValueError, OSError, RuntimeError) as error:
        return f"{SEARCH_ERROR_PREFIX}{error}"


def main() -> None:
    """命令行直接演示三个只读工具，不调用模型。"""
    parser = argparse.ArgumentParser(description="读取、列出或搜索项目内的文件。")
    parser.add_argument("target", help="文件路径；使用 --list/--grep 时填写匹配模式")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="列出匹配文件")
    mode.add_argument("--grep", action="store_true", help="搜索文件内容（正则表达式）")
    parser.add_argument("--path", default=".", help="列表或搜索的起始路径，默认项目根目录")
    parser.add_argument("--include", help="仅搜索符合文件名或相对路径 glob 的文件")
    parser.add_argument("--offset", type=int, default=1, help="起始行号，默认 1")
    parser.add_argument("--limit", type=int, default=200, help="最多返回行数，默认 200")
    parser.add_argument("--char-offset", type=int, help="从第几个字符读取；提供后改用字符模式，从 1 开始")
    parser.add_argument("--char-limit", type=int, default=4000, help="字符模式最多返回字符数，默认 4000")
    args = parser.parse_args()

    if args.grep:
        result = grep_search(args.target, path=args.path, include=args.include)
    elif args.list:
        result = list_files(args.target, path=args.path)
    else:
        result = read_file(args.target, offset=args.offset, limit=args.limit, char_offset=args.char_offset, char_limit=args.char_limit)
    if result.startswith((ERROR_PREFIX, LIST_ERROR_PREFIX, SEARCH_ERROR_PREFIX)):
        print(result, file=sys.stderr)
        raise SystemExit(1)
    print(result)


if __name__ == "__main__":
    main()
