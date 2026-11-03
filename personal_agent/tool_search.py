"""本次聊天的外部工具词法检索与激活状态，不启动或执行工具。"""

import json
from copy import deepcopy

from .skill_search import tokenize_skill_text


class DeferredToolCatalog:
    """目录只消费已发现的定义快照；激活状态归当前聊天所有，不使用全局变量。"""

    def __init__(self, definitions: list[dict], *, eager: bool = False):
        self._definitions = {tool["name"]: deepcopy(tool) for tool in definitions}
        self.eager = eager
        self._active: set[str] = set()
        self.reset()

    @property
    def active_names(self) -> set[str]:
        return set(self._active)

    def reset(self) -> None:
        self._active = set(self._definitions) if self.eager else set()

    def definitions(self) -> list[dict]:
        """只返回已激活定义的新副本，调用方每次发模型请求时重新取得。"""
        return [deepcopy(tool) for name, tool in self._definitions.items() if name in self._active]

    def find(self, query: str, limit: int = 3, *, excluded_names: set[str] | None = None) -> list[dict]:
        """按名称和描述匹配英文/中文词；返回有分数的元信息，不返回参数 Schema。"""
        if not isinstance(query, str) or not query.strip():
            raise ValueError("工具检索 query 必须是非空字符串。")
        if type(limit) is not int or not 1 <= limit <= 5:
            raise ValueError("工具检索 limit 必须是 1～5 的整数。")
        query = query.strip().casefold()
        tokens = set(tokenize_skill_text(query))
        hits = []
        for name, tool in self._definitions.items():
            if name in (excluded_names or set()):
                continue
            name_tokens = set(tokenize_skill_text(name))
            description_tokens = set(tokenize_skill_text(tool["description"]))
            matched = tokens & (name_tokens | description_tokens)
            score = sum(3 if term in name_tokens else 1 for term in matched)
            if query in name.casefold():
                score += 8
            if score:
                hits.append({"name": name, "description": tool["description"], "score": score, "matched_tokens": sorted(matched)})
        hits.sort(key=lambda hit: (-hit["score"], hit["name"]))
        return hits[:limit]

    def search(self, query: str, limit: int = 3, *, excluded_names: set[str] | None = None) -> str:
        """检索并合并激活项；不发送 MCP 请求，不授予外部操作权限。"""
        hits = self.find(query, limit, excluded_names=excluded_names)
        self._active.update(hit["name"] for hit in hits)
        return json.dumps({
            "tools": hits,
            "message": "已激活匹配工具；下一次模型请求将提供其参数 Schema，再按精确名称调用。激活不代表获准执行。" if hits else "没有匹配的可用 MCP 工具；可换用工具名或描述中的关键词。",
        }, ensure_ascii=False)
