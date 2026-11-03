"""有界读取 Python 项目文本，冻结源码字节与指纹；不执行源码。"""

import hashlib
import os
from pathlib import Path

from .file_runtime import plain_path
from .skill_task_eval import _fixture_name


PREFIX = "playground/source/"
SUFFIXES = {".py", ".ini", ".toml", ".cfg"}
EXCLUDED = {".git", ".venv", ".personal", "__pycache__", ".pytest_cache", ".idea", "node_modules", "build", "dist", "reference"}
MAX_FILES = 100
MAX_FILE_BYTES = 128 * 1024
MAX_TOTAL_BYTES = 1024 * 1024


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def normalized(raw):
    return raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")


def snapshot_sources(source_dir):
    root = plain_path(source_dir)
    if not root.is_dir():
        raise ValueError("源码目录不存在。")
    files, manifest, total = {}, {}, 0
    for current, directories, names in os.walk(root, followlinks=False):
        directories[:] = sorted(name for name in directories if name.casefold() not in EXCLUDED and not name.casefold().startswith(".env"))
        for name in directories:
            plain_path(Path(current) / name)
        for name in sorted(names):
            path = Path(current) / name
            if path.suffix.casefold() not in SUFFIXES or name.casefold().startswith(".env"):
                continue
            path = plain_path(path)
            if not path.is_file():
                raise ValueError("源码输入必须是普通文件。")
            relative = path.relative_to(root).as_posix()
            _fixture_name(PREFIX + relative)
            if len(files) >= MAX_FILES:
                raise ValueError("源码快照超过 100 个文本文件。")
            with path.open("rb") as source:
                raw = source.read(MAX_FILE_BYTES + 1)
            total += len(raw)
            if len(raw) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                raise ValueError("源码超过单文件 128 KiB 或总计 1 MiB 上限。")
            try:
                normalized(raw)
            except UnicodeError:
                raise ValueError(f"源码必须为 UTF-8 文本：{relative}") from None
            files[relative] = raw
            manifest[relative] = {"sha256": digest(raw), "bytes": len(raw)}
    if not any(Path(name).suffix.casefold() == ".py" for name in files):
        raise ValueError("源码快照至少需要一个 .py 文件。")
    return {"root": root, "files": files, "manifest": manifest}


def sources_match(source_dir, manifest):
    try:
        return snapshot_sources(source_dir)["manifest"] == manifest
    except (ValueError, OSError):
        return False


def encode_candidate(text, original):
    newline = "\r\n" if b"\r\n" in original else "\n"
    bom = b"\xef\xbb\xbf" if original.startswith(b"\xef\xbb\xbf") else b""
    return bom + text.replace("\n", newline).encode("utf-8")
