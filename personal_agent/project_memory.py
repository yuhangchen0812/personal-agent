"""项目长期记忆的选择模式、索引缓存与本地命令；不写入聊天历史。"""

from collections.abc import Callable
from dataclasses import dataclass, field
import sys

from .memory import (
    MemoryIndexCache, format_memory_freshness, list_memories, load_memory_index,
    read_memory, rebuild_memory_index, save_memory,
)


@dataclass
class ProjectMemoryState:
    selected: dict | None = None
    automatic: bool = False
    index_cache: MemoryIndexCache = field(default_factory=MemoryIndexCache)

    def reset(self) -> None:
        self.selected = None
        self.automatic = False
        self.index_cache.clear()

    def select_for_turn(
        self, user_input: str, *, selector: Callable[..., dict | None], before_select: Callable[[], None],
    ) -> None:
        """先释放上一轮选择，再结算预算；失败时不沿用旧记忆。"""
        self.selected = None
        before_select()
        try:
            self.selected = selector(user_input, index_cache=self.index_cache)
        except (ValueError, OSError) as error:
            self.index_cache.clear()
            print(f"记忆选择失败：{error} 本轮未加载项目记忆。", file=sys.stderr)
        else:
            print(f"自动选用项目记忆：{self.selected['name']}。" if self.selected else "未匹配到相关项目记忆。")
            if self.selected is not None:
                print(format_memory_freshness(self.selected))

    def handle_command(self, user_input: str) -> None:
        """沿用现有 /memory 命令；只管理选择、缓存和显式存储操作。"""
        parts = user_input.split(maxsplit=3)
        try:
            if len(parts) == 1:
                entries = list_memories()
                print("项目记忆：\n" + "\n".join(f"- {entry['name']} [{entry['type']}] 更新于 {entry['updated_at']}（UTC）" for entry in entries) if entries else "当前项目暂无长期记忆。")
                print(f"当前选用：{self.selected['name']}。" if self.selected else "当前未选用记忆。")
                if self.selected is not None:
                    print(format_memory_freshness(self.selected))
                print("记忆模式：自动，每轮重新匹配。" if self.automatic else "记忆模式：手动。")
            elif len(parts) == 4 and parts[1].lower() == "save":
                self.index_cache.clear()
                save_memory(parts[2], parts[3])
                print(f"已保存项目记忆：{parts[2]}。")
            elif len(parts) == 3 and parts[1].lower() == "read":
                entry = read_memory(parts[2])
                print(f"项目记忆：{entry['name']} [{entry['type']}]\n{format_memory_freshness(entry)}\n{entry['content']}")
            elif len(parts) == 3 and parts[1].lower() == "use":
                self.selected = read_memory(parts[2])
                self.automatic = False
                self.index_cache.clear()
                print(f"已选用项目记忆：{self.selected['name']}；/memory off 停止使用。")
                print(format_memory_freshness(self.selected))
            elif len(parts) == 2 and parts[1].lower() == "auto":
                self.selected = None
                self.automatic = True
                self.index_cache.clear()
                print("已开启自动记忆选择，每轮最多匹配一条；/memory off 关闭。")
                try:
                    self.index_cache.load()
                except (ValueError, OSError) as error:
                    print(f"记忆索引预取失败：{error} 后续提问时会重试加载。", file=sys.stderr)
            elif len(parts) == 2 and parts[1].lower() == "off":
                self.selected = None
                self.automatic = False
                self.index_cache.clear()
                print("已关闭记忆选用及自动选择；已有聊天历史保留。")
            elif len(parts) == 2 and parts[1].lower() == "index":
                self.index_cache.clear()
                rebuild_memory_index()
                print(load_memory_index())
            else:
                print("用法：/memory；/memory save 名称 正文；/memory read 名称；/memory use 名称；/memory auto；/memory off；/memory index。")
        except (ValueError, OSError) as error:
            print(f"记忆操作失败：{error}", file=sys.stderr)
