"""解析单行 key: value 形式的 frontmatter 子集，不是通用 YAML 解析器。"""

import json
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class FrontmatterResult:
    meta: dict[str, str]
    body: str


def parse_frontmatter(content: str) -> FrontmatterResult:
    """返回元信息和 Markdown 正文；没有头部时按纯正文处理。"""
    content = content.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = content.split("\n")
    if lines[0].strip() != "---":
        return FrontmatterResult({}, content.strip("\n"))
    end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if end is None:
        raise ValueError("frontmatter 的 --- 头部未闭合。")
    meta = {}
    for number, line in enumerate(lines[1:end], start=2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, colon, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if line[0].isspace() or not colon or not re.fullmatch(r"[a-z][a-z0-9_-]*", key) or key in meta:
            raise ValueError(f"frontmatter 第 {number} 行必须为不重复的顶层 key: value。")
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except ValueError:
                raise ValueError(f"frontmatter 第 {number} 行的双引号字符串无效。") from None
        elif value.startswith("'"):
            if re.fullmatch(r"'(?:[^']|'')*'", value) is None:
                raise ValueError(f"frontmatter 第 {number} 行的单引号字符串无效。")
            value = value[1:-1].replace("''", "'")
        elif value.startswith(("|", ">")):
            raise ValueError("当前 frontmatter 只支持单行值，不支持多行块。")
        meta[key] = value
    return FrontmatterResult(meta, "\n".join(lines[end + 1:]).strip("\n"))
