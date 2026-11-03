"""对话历史、轮次检查点与独立会话记忆的运行状态。"""

from collections.abc import Callable
from dataclasses import dataclass, field
import json
import sys

from .context import AutoCompactPolicy, compact_context, context_chars
from .model_errors import MODEL_API_ERRORS
from .plan import PlanSession
from .session import Session
from .session_memory import SessionMemoryStore, compacted_history, generate_memory, with_session_memory


ModelCallback = Callable[[list[dict]], dict]
MemoryGenerator = Callable[..., tuple[dict, list[dict], int] | None]


@dataclass
class ConversationState:
    """共享同一份历史；检查点回退只删除消息，不撤销文件或进程副作用。

    session 和 plan 来自本次运行。记忆只是参考，状态对象不会重放工具或恢复授权。
    """

    messages: list[dict]
    session: Session | None
    plan: PlanSession
    auto_compact: AutoCompactPolicy
    memory_store: SessionMemoryStore | None
    memory_record: dict | None = None
    turn_start: int = field(init=False)
    compaction_requested: bool = False

    def __post_init__(self) -> None:
        self.checkpoint()

    def checkpoint(self) -> None:
        """把当前完整历史记为下一次回退的边界。"""
        self.turn_start = len(self.messages)

    def begin_turn(self) -> None:
        self.checkpoint()
        self.compaction_requested = False

    @property
    def completed_history(self) -> list[dict]:
        return self.messages[:self.turn_start]

    @property
    def turn_messages(self) -> list[dict]:
        return self.messages[self.turn_start:]

    def rollback_turn(self) -> None:
        del self.messages[self.turn_start:]

    def replace_history(self, candidate: list[dict]) -> None:
        """保留历史列表的身份，供主循环及工具回调继续共享。"""
        self.messages[:] = candidate
        self.checkpoint()

    def save_empty_history(self) -> None:
        """先尝试保存空历史；成功后由调用方清理运行状态。"""
        if self.session is not None:
            snapshot = self.plan.snapshot() if self.session.with_tools else None
            self.session.save([], plan=snapshot, session_memory=None)

    def clear_history(self) -> None:
        self.messages.clear()
        self.memory_record = None
        self.turn_start = 0

    @property
    def compaction_paused(self) -> bool:
        return (
            self.session is not None
            and self.session.session_memory is not None
            and not self.session.session_memory["enabled"]
        )

    def publish_memory(
        self, model: ModelCallback, *, compact: bool, generate: MemoryGenerator = generate_memory,
    ) -> bool:
        """三种折叠入口与显式 save/compact 共用；引用发布成功后才切换运行状态。"""
        reference = self.session.session_memory
        previous = self.memory_store.read(reference) if reference is not None else None
        generated = generate(self.messages, model, previous=previous, compact=compact)
        if generated is None:
            return False
        memory, history, source_count = generated
        reference = self.memory_store.save(
            memory, history, source_count, compact=compact, previous_id=previous["id"] if previous else None,
        )
        record = self.memory_store.read(reference)
        candidate = compacted_history(history, record, previous=self.memory_record) if compact else history
        snapshot = self.plan.snapshot() if self.session.with_tools else None
        before = context_chars(with_session_memory(self.messages, self.memory_record))
        self.session.save(candidate, plan=snapshot, session_memory=reference)
        self.replace_history(candidate)
        self.memory_record = record
        if compact:
            after = context_chars(with_session_memory(self.messages, record))
            print(f"上下文已折叠：JSON 字符数（含独立记忆参考）{before} → {after}；最近一轮保留。")
        print(f"会话记忆已保存：{reference['id']}；启用；未执行记忆中的工具或计划。")
        print(f"来源快照：{record['source_archive']}")
        return True

    def compact(self, model: ModelCallback, *, generate: MemoryGenerator = generate_memory) -> bool:
        """具名会话使用独立记忆；无具名聊天保留原六字段摘要。"""
        if self.session is not None:
            if self.compaction_paused:
                raise ValueError("独立会话记忆已停用；先 /session-memory use，或显式 /session-memory compact。")
            return self.publish_memory(model, compact=True, generate=generate)
        result = compact_context(
            self.messages, model,
        )
        if result is None:
            return False
        candidate, archive = result
        before, after = context_chars(self.messages), context_chars(candidate)
        self.replace_history(candidate)
        print(f"上下文已折叠：JSON 字符数 {before} → {after}；最近一轮保留。")
        print(f"原历史备份：{archive}")
        return True

    def handle_memory_command(
        self, user_input: str, model: ModelCallback, *, generate: MemoryGenerator = generate_memory,
    ) -> None:
        """处理会话记忆命令，保存成功后才切换运行中的记忆引用。"""
        parts = user_input.split()
        action = parts[1].lower() if len(parts) > 1 else "show"
        if (action not in {"show", "save", "compact", "off", "use", "clear"}
                or len(parts) > (3 if action == "use" else 2)):
            print("用法：/session-memory show|save|compact|off|use [记录ID]|clear。")
            return
        if self.session is None:
            print("独立 session-memory 需要 --session NAME 或 --resume NAME。", file=sys.stderr)
            return
        try:
            reference = self.session.session_memory
            if action == "show":
                if reference is None:
                    print("当前会话没有绑定 session-memory；save 保留历史，compact 折叠旧轮次。")
                else:
                    record = self.memory_store.read(reference)
                    print(f"独立会话记忆：{reference['id']}；{'启用' if reference['enabled'] else '停用'}。")
                    print(json.dumps(record["memory"], ensure_ascii=False, indent=2))
                return
            candidate, record = self.messages, None
            if action in {"save", "compact"}:
                try:
                    if not self.publish_memory(model, compact=action == "compact", generate=generate):
                        print("没有新增的已完成旧轮次可折叠；原历史和记忆保持不变。")
                finally:
                    if action == "compact":
                        self.auto_compact.remember(self.messages)
                return
            elif action == "clear":
                reference = None
            elif action == "off":
                if reference is None:
                    raise ValueError("当前没有绑定 session-memory。")
                reference = {**reference, "enabled": False}
            elif action == "use":
                reference = self.memory_store.reference(parts[2]) if len(parts) == 3 else ({**reference, "enabled": True} if reference else None)
                record = self.memory_store.read(reference)
            snapshot = self.plan.snapshot() if self.session.with_tools else None
            # 不可变记录可先写入；只有会话引用成功替换后，才改变运行中的历史/上下文。
            self.session.save(candidate, plan=snapshot, session_memory=reference)
            self.replace_history(candidate)
            self.memory_record = record if reference is not None and reference["enabled"] else None
            print("独立会话记忆已解除绑定；历史记录保留。" if reference is None else
                  f"会话记忆已保存：{reference['id']}；{'启用' if reference['enabled'] else '停用'}；未执行记忆中的工具或计划。")
        except KeyboardInterrupt:
            print("\n会话记忆操作已中断；请查看绑定和历史，已落盘的记录保留。")
        except MODEL_API_ERRORS as error:
            print(f"会话记忆模型请求失败（{type(error).__name__}），当前绑定和历史未改变。", file=sys.stderr)
        except (ValueError, OSError, RuntimeError) as error:
            print(f"会话记忆操作失败：{error}", file=sys.stderr)
