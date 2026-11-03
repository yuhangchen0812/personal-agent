"""将过长的工具结果落盘，给历史保留预览和可读取的完整结果路径。"""

import re
import tempfile
from pathlib import Path

from .cancellation import check_cancelled
from .workspace import project_root


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAX_TOOL_RESULT_CHARS = 12_000
PREVIEW_HEAD_CHARS = 3_000
PREVIEW_TAIL_CHARS = 1_000
OLD_TOOL_RESULT_CHARS = 1_000
OLD_TOOL_PREVIEW_CHARS = 200


def is_tool_result_path(relative: Path) -> bool:
    """仅识别运行时生成的结果文件，不开放整个 .personal 目录。"""
    parts = [part.casefold() for part in relative.parts]
    return (
        len(parts) == 3 and parts[:2] == [".personal", "tool-results"]
        and re.fullmatch(r"result-[a-z0-9_]+\.txt", parts[2]) is not None
    )


def _save_full_result(result: str) -> Path:
    root = project_root(PROJECT_ROOT)
    directory = root / ".personal" / "tool-results"
    if directory.resolve() != directory:
        raise ValueError("工具结果目录不能重定向。")
    directory.mkdir(parents=True, exist_ok=True)
    path = None
    completed = False
    try:
        check_cancelled()
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=directory,
            prefix="result-", suffix=".txt", delete=False,
        ) as target:
            path = Path(target.name)
            target.write(result)
        check_cancelled()
        completed = True
        return path
    finally:
        if path is not None and not completed:
            path.unlink(missing_ok=True)


def _result_preview(result: str, relative: str) -> str:
    """统一生成首尾预览，也用于核对旧预览的完整结果引用。"""
    return (
        f"[工具结果已截断：原文 {len(result)} 字符，仅预览前 {PREVIEW_HEAD_CHARS} 和后 {PREVIEW_TAIL_CHARS} 字符。]\n"
        f"完整结果：{relative}\n"
        f'可用 read_file(file_path="{relative}", char_offset=1, char_limit=4000) 分段读取。\n\n'
        + result[:PREVIEW_HEAD_CHARS] + "\n\n[……中间内容已省略……]\n\n" + result[-PREVIEW_TAIL_CHARS:]
    )


def prepare_tool_result(result: str) -> str:
    """短结果原样返回；长结果保存成功才截断，保存失败保留全文并提示。"""
    if len(result) <= MAX_TOOL_RESULT_CHARS:
        return result
    try:
        path = _save_full_result(result)
    except (ValueError, OSError) as error:
        return f"[完整结果保存失败（{type(error).__name__}）；保留原结果，未截断。]\n{result}"
    return _result_preview(result, path.relative_to(project_root(PROJECT_ROOT)).as_posix())


def _existing_full_result(result: str) -> tuple[str, int] | None:
    """识别已有大结果预览，仅复用本项目内仍存在的普通结果文件路径。"""
    lines = result.split("\n", 2)
    if len(lines) < 3:
        return None
    header = re.fullmatch(r"\[工具结果已截断：原文 (\d+) 字符，仅预览前 \d+ 和后 \d+ 字符。\]", lines[0])
    if header is None or not lines[1].startswith("完整结果："):
        return None
    relative = Path(lines[1].removeprefix("完整结果："))
    if not is_tool_result_path(relative):
        return None
    path = project_root(PROJECT_ROOT) / relative
    if path.resolve() != path or not path.is_file():
        return None
    with path.open(encoding="utf-8", newline="") as source:
        full_result = source.read()
    if _result_preview(full_result, relative.as_posix()) != result:
        return None  # 不凭一行路径就丢掉旧内容；无法核对时另存当前消息。
    return relative.as_posix(), len(full_result)


def shrink_old_tool_results(messages: list[dict]) -> int:
    """原地收缩最近 user 之前的长工具结果，保留消息结构；返回收缩条数。"""
    latest_user = next((index for index in range(len(messages) - 1, -1, -1) if messages[index].get("role") == "user"), -1)
    changed = 0
    for index in range(latest_user):
        check_cancelled()
        message = messages[index]
        result = message.get("content")
        if message.get("role") != "tool" or not isinstance(result, str) or len(result) <= OLD_TOOL_RESULT_CHARS:
            continue
        try:
            reference = _existing_full_result(result)
            if reference is None:
                path = _save_full_result(result)
                reference = path.relative_to(project_root(PROJECT_ROOT)).as_posix(), len(result)
        except (ValueError, OSError):
            continue  # 无法保存时保留旧内容，不把历史变成无法回读的空壳。
        relative, original_chars = reference
        content = (
            f"[旧工具结果已收缩：完整内容 {original_chars} 字符，仅保留简短预览。]\n"
            f"完整结果：{relative}\n"
            f'可用 read_file(file_path="{relative}", char_offset=1, char_limit=4000) 回读。\n\n'
            + result[:OLD_TOOL_PREVIEW_CHARS]
        )
        messages[index] = {**message, "content": content}
        changed += 1
    return changed
