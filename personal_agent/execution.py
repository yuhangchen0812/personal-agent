"""模型请求路由、工具暴露上下文和轮次收尾；权限判断仍由工具入口执行。"""

from collections.abc import Callable
from dataclasses import dataclass, field
import sys

from .agent_config import AgentDefinition
from .conversation import ConversationState
from .memory import with_memory_context
from .project_memory import ProjectMemoryState
from .session import validate_text_messages
from .session_memory import with_session_memory
from .single_run import SingleRun
from .skill_search import with_skill_context
from .tool_registry import with_custom_agent_definitions
from .tool_search import DeferredToolCatalog
from .usage import UsageCallback, UsageStats


@dataclass
class RequestContext:
    """每次请求重新组装参考上下文，记录本次实际暴露的 MCP 工具。"""

    conversation: ConversationState
    project_memory: ProjectMemoryState
    text_mode: bool
    tool_catalog: DeferredToolCatalog | None
    custom_agents: dict[str, AgentDefinition] = field(default_factory=dict)
    exposed_mcp_tools: set[str] = field(default_factory=set)
    skill_context: str = ""

    def prepare(self, messages: list[dict], definitions: list[dict]) -> tuple[list[dict], list[dict]]:
        if not self.text_mode:
            definitions = with_custom_agent_definitions(definitions, self.custom_agents)
            if self.tool_catalog is not None:
                active_definitions = self.tool_catalog.definitions()
                self.exposed_mcp_tools = {tool["name"] for tool in active_definitions}
                definitions = [*definitions, *active_definitions]
            else:
                definitions = [tool for tool in definitions if tool["name"] != "tool_search"]
        messages = with_session_memory(messages, self.conversation.memory_record)
        if self.project_memory.selected is not None:
            messages = with_memory_context(messages, self.project_memory.selected)
        messages = with_skill_context(messages, self.skill_context)
        if not self.text_mode:
            if self.conversation.session is not None:
                context = f"当前权限模式：{self.conversation.plan.mode}。恢复的工具消息仅为历史，不要重放旧调用；历史批准不代表新操作已获授权。"
                messages = [{"role": "system", "content": context}, *messages]
            if self.conversation.plan.plan_file is not None:
                messages = [{"role": "system", "content": self.conversation.plan.describe()}, *messages]
        return messages, definitions


@dataclass
class ModelRequests:
    """统一请求前预算检查和 finally 用量累计；不同用途保留各自的上下文范围。"""

    context: RequestContext
    stream: bool
    evaluation_settings: dict
    usage_stats: UsageStats
    before_request: Callable[[], None]
    raw_model: Callable[..., dict]
    stream_model: Callable[..., dict]
    model_with_progress: Callable[..., dict]

    def request(self, messages: list[dict], definitions: list[dict], on_usage: UsageCallback) -> dict:
        messages, definitions = self.context.prepare(messages, definitions)
        if self.stream:
            started = False

            def show_text(text: str) -> None:
                nonlocal started
                if not started:
                    print("Personal Agent > ", end="", flush=True)
                    started = True
                print(text, end="", flush=True)

            try:
                if self.context.text_mode:
                    message = self.stream_model(messages, on_text=show_text, on_usage=on_usage)
                    validate_text_messages([messages[-1], message])
                else:
                    message = self.stream_model(messages, definitions, on_text=show_text, on_usage=on_usage)
            finally:
                if started:
                    print(flush=True)
            for call in message.get("tool_calls", []):
                print(f"模型请求工具：{call['function']['name']}")
            return message
        if self.context.text_mode:
            # 不发送工具定义，并在交给循环前拒绝非文本响应。
            message = self.raw_model(messages, on_usage=on_usage)
            validate_text_messages([messages[-1], message])
            return message
        return self.model_with_progress(messages, definitions, on_usage=on_usage)

    def call(
        self, messages: list[dict], definitions: list[dict], *, for_compaction: bool = False,
        for_subagent: bool = False, for_skill_draft: bool = False, for_skill_eval: bool = False,
        for_skill_judge: bool = False, for_skill_task_eval: bool = False,
        on_usage_observer: UsageCallback | None = None,
    ) -> dict:
        # 未发出的请求不计为缺失用量；失败和取消也保留已报告用量。
        self.before_request()
        reported = None

        def capture_usage(usage: dict | None) -> None:
            nonlocal reported
            reported = usage

        try:
            if for_skill_task_eval:
                print("正在回放文件工具任务……")
                return self.raw_model(messages, definitions, on_usage=capture_usage, max_tokens=self.evaluation_settings["max_tokens"])
            if for_compaction or for_skill_draft or for_skill_eval or for_skill_judge:
                print("正在评审回放回答……" if for_skill_judge else "正在回放技能方法……" if for_skill_eval else "正在生成技能候选草稿……" if for_skill_draft else "正在生成接续摘要……")
                return self.raw_model(messages, on_usage=capture_usage, max_tokens=self.evaluation_settings["max_tokens"])
            if for_subagent:
                # 子任务使用自己的消息和非流式响应，共用同一预算检查及 finally 用量累计。
                return self.model_with_progress(messages, definitions, on_usage=capture_usage)
            return self.request(messages, definitions, capture_usage)
        finally:
            # 失败的对话可以回滚，但已收到的用量不能随之抹掉。
            self.usage_stats.record(reported)
            if on_usage_observer is not None:
                on_usage_observer(reported)


@dataclass
class TurnLifecycle:
    """集中处理保存、审批前检查点及任务结束，不撤销工具已产生的副作用。"""

    conversation: ConversationState
    single_run: SingleRun | None
    confirm_callback: Callable[[str, dict, str], bool]
    finish_task: Callable[..., None]

    def save_progress(self, history: list[dict]) -> None:
        if self.conversation.session is not None:
            try:
                snapshot = self.conversation.plan.snapshot() if self.conversation.session.with_tools else None
                self.conversation.session.save(history, plan=snapshot)
                if self.single_run is not None:
                    self.single_run.session["saved"] = True
            except (ValueError, OSError, RuntimeError) as error:
                if self.single_run is not None:
                    self.single_run.session["saved"] = False
                    self.single_run.fail(error, phase="session_save")
                print(f"会话保存失败：{error} 本轮仍在内存中，磁盘存档未更新。", file=sys.stderr)

    def confirm_with_checkpoint(self, name: str, arguments: dict, reason: str) -> bool:
        if self.single_run is not None:
            print(f"单次模式不读取审批输入，拒绝本次确认：{name}。", file=sys.stderr)
            return False
        if name == "exit_plan_mode" and self.conversation.session is not None:
            self.save_progress(self.conversation.completed_history)
        return self.confirm_callback(name, arguments, reason)

    def fail_turn(self, error: BaseException, *, cancelled: bool = False) -> None:
        self.finish_task("cancelled" if cancelled else "failed")
        if self.single_run is not None:
            self.single_run.capture(self.conversation.turn_messages)
            if cancelled:
                self.single_run.fail(error, cancelled=True)
            else:
                self.single_run.fail(error)
        self.conversation.rollback_turn()
        if self.conversation.session is not None and self.conversation.session.with_tools:
            self.save_progress(self.conversation.messages)

    def complete_turn(self, answer: str) -> None:
        self.save_progress(self.conversation.messages)
        self.finish_task("loop_completed", answer)
        if self.single_run is not None:
            self.single_run.complete(answer, self.conversation.turn_messages)
