"""一次运行内的 Skill 解析/词法索引缓存；不持久化，不缓存模型回答。"""

from contextlib import contextmanager
from contextvars import ContextVar
from threading import RLock


_active_cache = ContextVar("personal_agent_skill_cache", default=None)


class SkillCache:
    def __init__(self):
        self._definitions = {}
        self._index_key = None
        self._index = None
        self._lock = RLock()
        self._counts = dict.fromkeys(("definition_loads", "definition_hits", "index_builds", "index_hits", "invalidations"), 0)

    def definition(self, key, signature, load):
        """每次验证路径/文件状态；加载失败或加载期间改变时移除旧条目。"""
        with self._lock:
            try:
                before = signature()
                previous = self._definitions.get(key)
                if previous is not None and previous[0] == before:
                    self._counts["definition_hits"] += 1
                    return previous[1]
                self._definitions.pop(key, None)
                self._counts["definition_loads"] += 1
                definition = load()
                if signature() != before:
                    raise ValueError("Skill 文件在发现期间发生变化，本次不使用该定义，请重试。")
                self._definitions[key] = (before, definition)
                return definition
            except BaseException:
                self._definitions.pop(key, None)
                raise

    def index(self, key, build):
        """只保留最近一组完整定义的分词索引；查询和排序结果不缓存。"""
        with self._lock:
            if self._index_key == key and self._index is not None:
                self._counts["index_hits"] += 1
                return self._index
            self._index_key, self._index = None, None
            self._counts["index_builds"] += 1
            index = build()
            self._index_key, self._index = key, index
            return index

    def clear(self):
        """清除两层内容，保留本次运行的累计计数。"""
        with self._lock:
            self._definitions.clear()
            self._index_key, self._index = None, None
            self._counts["invalidations"] += 1

    def info(self):
        with self._lock:
            return {**self._counts, "cached_files": len(self._definitions), "index_ready": self._index is not None}

    def describe(self):
        info = self.info()
        return (f"Skill 缓存（本次运行）：解析尝试 {info['definition_loads']} 次，定义命中 {info['definition_hits']} 次；"
                f"索引构建 {info['index_builds']} 次，索引复用 {info['index_hits']} 次；清除 {info['invalidations']} 次。\n"
                f"缓存文件条目 {info['cached_files']}；词法索引{'已就绪' if info['index_ready'] else '未构建'}。"
                "计数范围为发现解析和词法索引；实际技能调用另外重读正文。")


def current_skill_cache():
    return _active_cache.get()


def reset_skill_cache():
    cache = current_skill_cache()
    if cache is not None:
        cache.clear()


@contextmanager
def skill_cache_scope(cache=None):
    """每次聊天独立，嵌套退出恢复；后台可沿用已有 copy_context。"""
    cache = SkillCache() if cache is None else cache
    token = _active_cache.set(cache)
    try:
        yield cache
    finally:
        _active_cache.reset(token)
