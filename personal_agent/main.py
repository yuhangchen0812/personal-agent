"""Personal Agent 的最小命令行入口。"""

import argparse
import json
import sys
from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path

from .model_errors import MODEL_API_ERRORS

from .agent import run_agent_loop
from .agent_config import AgentDefinition, BUILTIN_AGENT_TYPES, discover_agents
from .context import AutoCompactPolicy, compact_context, context_chars
from .config import get_model_settings, model_config_scope
from .cancellation import termination_requested, termination_scope
from .llm import MAX_REQUEST_RETRIES, get_model_message, stream_model_message, stream_model_reply
from .memory import (
    MemoryIndexCache, format_memory_freshness, list_memories, load_memory_index,
    read_memory, rebuild_memory_index, save_memory, select_relevant_memory, with_memory_context,
)
from .permissions import PERMISSION_MODES, READ_TOOLS, SUPPORTED_TOOLS, confirm_in_terminal
from .mcp_client import McpError
from .mcp_runtime import McpToolRuntime, load_mcp_config
from .plan import PlanSession
from .session import Session, validate_text_messages
from .session_memory import SessionMemoryStore, compacted_history, generate_memory, with_session_memory
from .single_run import SingleRun, print_report, run_once
from .skill_search import (
    format_retrieved_skill_context, format_skill_search, retrieve_relevant_skills, with_skill_context,
)
from .skill_evolution import SkillEvolutionStore
from .skill_eval import SkillEvaluator, format_evaluation
from .skill_background import SkillDraftJobs, format_skill_job
from .skill_cache import current_skill_cache, reset_skill_cache, skill_cache_scope
from .skill_decision import format_decision
from .skill_lifecycle import SkillLifecycleStore, format_skill_statistics, record_skill_event
from .skill_learning import SkillLearning, list_skill_tasks, skill_learning_scope
from .skills import (
    BUILTIN_COMMAND_NAMES, PreparedSkillCall, create_project_skill, discover_skills, evolve_project_skill,
    prepare_skill_call,
)
from .subagent import PlanFormatError, run_custom_agent, run_explore_agent, run_fork_skill, run_general_agent, run_plan_agent
from .tool_registry import TOOL_ERROR_PREFIX, execute_tool, with_custom_agent_definitions
from .tool_search import DeferredToolCatalog
from .usage import TokenPrices, UsageCallback, UsageStats, parse_token_price


def _call_model_with_progress(messages: list[dict], definitions: list[dict], on_usage: UsageCallback | None = None) -> dict:
    """请求真实模型，并显示它提出的工具请求名称。"""
    message = get_model_message(messages, definitions, on_usage=on_usage)
    for call in message.get("tool_calls", []):
        print(f"模型请求工具：{call['function']['name']}")
    return message


def run_repl(
    max_turns: int = 5, permission_mode: str = "default",
    allow: list[str] | None = None, deny: list[str] | None = None,
    *, session_name: str | None = None, resume: bool = False, with_tools: bool = False,
    stream: bool = False, prices: TokenPrices | None = None, max_cost: Decimal | None = None,
    auto_compact_chars: int | None = None,
    mcp_config: str | Path | None = None,
    mcp_eager: bool = False,
    single_run: SingleRun | None = None,
    model_overrides: dict | None = None,
) -> list[dict]:
    """聊天拥有外部进程生命周期；正常退出、异常和 Ctrl+C 都经过资源清理。"""
    with ExitStack() as resources:
        if model_overrides is not None:
            resources.enter_context(model_config_scope(model_overrides))
        resources.enter_context(skill_cache_scope())
        return _run_repl(
            max_turns, permission_mode, allow, deny, session_name=session_name,
            resume=resume, with_tools=with_tools, stream=stream, prices=prices,
            max_cost=max_cost, auto_compact_chars=auto_compact_chars,
            mcp_config=mcp_config, mcp_eager=mcp_eager, resources=resources,
            single_run=single_run,
        )


def _run_repl(
    max_turns: int, permission_mode: str, allow: list[str] | None, deny: list[str] | None,
    *, session_name: str | None, resume: bool, with_tools: bool, stream: bool,
    prices: TokenPrices | None, max_cost: Decimal | None, auto_compact_chars: int | None,
    mcp_config: str | Path | None, mcp_eager: bool, resources: ExitStack,
    single_run: SingleRun | None = None,
) -> list[dict]:
    """循环处理输入，并返回本次运行中的对话消息。"""
    model_settings = get_model_settings()
    evaluation_settings = {
        **model_settings, "max_tokens": 1024, "stream": False,
        "temperature": "provider_default", "max_request_retries": MAX_REQUEST_RETRIES,
        **({"enable_thinking": model_settings.get("thinking", {}).get("type") == "enabled"} if model_settings["protocol"] == "openai"
           else {"thinking": model_settings.get("thinking", {"type": "disabled"})}),
    }
    evaluation_settings["max_tokens"] = model_settings.get("max_tokens", 1024)
    auto_compact = AutoCompactPolicy(auto_compact_chars)
    if mcp_eager and mcp_config is None:
        raise ValueError("--mcp-eager 需要同时提供 --mcp-config。")
    if max_cost is not None:
        if not isinstance(max_cost, Decimal) or not max_cost.is_finite() or max_cost < 0:
            raise ValueError("max_cost 必须是非负有限 Decimal，单位为元。")
        if prices is None:
            raise ValueError("启用 --max-cost 必须同时配置 --input-price 和 --output-price。")
    if resume and session_name is None:
        raise ValueError("恢复会话需要提供会话名。")
    if with_tools and (session_name is None or resume):
        raise ValueError("--with-tools 仅用于 --session 新建工具会话；恢复时自动识别类型。")
    if stream and permission_mode == "plan" and session_name is None and single_run is None:
        raise ValueError("流式 Plan 请使用 --session NAME --with-tools 创建工具会话。")
    session = Session(session_name, with_tools=with_tools) if session_name is not None else None
    messages: list[dict] = []
    if session is not None and resume:
        messages = session.load()
    session_memory_store = SessionMemoryStore(session) if session is not None else None
    session_memory_record = (session_memory_store.read(session.session_memory)
        if session is not None and session.session_memory is not None and session.session_memory["enabled"] else None)
    text_mode = (session is not None and not session.with_tools) or (stream and session is None and single_run is None)
    if mcp_config is not None and text_mode:
        raise ValueError("MCP 需要工具聊天；流式或具名会话请使用 --session NAME --with-tools。")
    if session is not None:
        if permission_mode == "plan" and not session.with_tools:
            raise ValueError("普通文本会话不能使用 Plan；新建时请加 --with-tools。")
        if not resume:
            messages = session.create()
    plan = PlanSession(permission_mode)
    if resume and session is not None and session.plan_snapshot is not None:
        plan.restore(session.plan_snapshot)
    usage_stats = UsageStats()
    evolution = SkillEvolutionStore()
    evaluator = SkillEvaluator()
    lifecycle = SkillLifecycleStore()
    selected_memory: dict[str, str] | None = None
    auto_memory = False
    memory_index_cache = MemoryIndexCache()
    skill_context = ""
    custom_agents: dict[str, AgentDefinition] = {}
    mcp = resources.enter_context(McpToolRuntime(load_mcp_config(mcp_config))) if mcp_config is not None else None
    tool_catalog = DeferredToolCatalog(mcp.definitions(), eager=mcp_eager) if mcp is not None else None
    exposed_mcp_tools: set[str] = set()
    supported = SUPPORTED_TOOLS | (mcp.names if mcp is not None else set())
    for rules in (allow, deny):
        if rules is not None and (not isinstance(rules, list) or any(not isinstance(name, str) or name not in supported for name in rules)):
            raise ValueError("allow/deny 必须使用当前已注册的精确工具名；MCP 名称可通过 /mcp 查看。")
    compaction_requested = False
    if single_run is not None:
        single_run.bind(model_settings, usage_stats, prices, max_cost, session, resume)
    print("Personal Agent 已启动。" if single_run is None else "Personal Agent 单次任务已启动。")
    print(f"模型协议：{model_settings['protocol']}；模型：{model_settings['model']}。")
    if model_settings.get("anthropic_provider") == "qwen":
        print("Messages 服务：千问兼容网关；max_tokens 限正文，思考预算另计；网关空签名不代表原生签名验收。")
    if model_settings["protocol"] == "anthropic" or "thinking" in model_settings:
        print(f"思考模式：{model_settings.get('thinking', {'type': 'disabled'})['type']}；配置输出上限：{model_settings.get('max_tokens', '按请求入口默认值')}。")
    print(f"每轮父任务及每个子任务各最多请求模型 {max_turns} 次（折叠、技能草稿与回放另计，不含请求内部重试）；费用预算共享。")
    print(f"权限模式：{plan.mode}。")
    if mcp is not None:
        loading = "全量提供定义" if mcp_eager else "默认延迟定义，模型先 tool_search 检索激活"
        print(f"MCP 已连接 {len(mcp.clients)} 个本地 Server，发现 {len(mcp.names)} 个工具；{loading}；/mcp 查看名称，调用仍按权限审批。")
    if auto_compact_chars is not None:
        print(f"自动折叠已启用：历史 JSON 达到 {auto_compact_chars} 字符时，在新问题开始前检查。")
    if max_cost is not None:
        print(f"本次运行估算预算：{max_cost} 元；缺少完整用量时停止后续调用。")
    if stream:
        kind = "普通文本" if text_mode else "工具与文本"
        print(f"{kind}流式输出已启用。")
    if session is not None:
        kind = "工具" if session.with_tools else "普通文本"
        turns = sum(message["role"] == "user" for message in messages)
        print(f"{kind}会话：{session.name}；{'恢复' if resume else '新建'} {turns} 轮历史。")
        print(f"存档：{session.path}。")
    if single_run is None:
        print("输入内容后按回车；/agents 列出自定义 Agent 配置（项目优先）；/skills 列出技能；/skills search 查询 检索技能；/skills cache 查看缓存；/skills refresh 刷新缓存；/skill-create 创建项目技能；/skill-evolve 追加技能经验并备份；/技能名 参数 或 /skill 路径 参数 调用技能；/compact 折叠旧对话；/memory 管理项目记忆；/clear 清空历史；/usage 查看用量；/cost 查看估算费用；exit 退出。")
        print("规划入口：/plan 进入规划或提交实际计划审批；/plan status 查看状态；/plan submit 明确提交；纯文本聊天不能切换 Plan。")
    if plan.mode == "plan":
        print(plan.describe())
    if single_run is None:
        print("技能反馈：/feedback [技能名 反馈内容]；/skill-feedback 技能名 评分 [说明] 保存评分；/skill-draft 反馈ID；/skill-decisions [决策ID]；/skill-candidates [候选ID]；/skill-adopt 候选ID；/skill-reject 候选ID。")
        print("技能回放：/skill-eval 候选ID 样本JSON路径；/skill-eval-judge 同参数启用裁判；/skill-eval-tools 同参数执行文件任务；/skill-evals [评估ID]；/skill-champions [评估ID] 查看留档。")
        print("技能生命周期：/skill-stats [技能名]；/skill-archive 技能名 [原因]；/skill-archives；/skill-restore 归档ID。")
        print("后台反馈：/skill-learn 技能名 开启一次性窗口；/skill-learn auto [技能名] 开启连续学习，下一输入仍照常聊天；/skill-learn off 关闭；/skill-jobs [wait|cancel 任务ID|任务ID]。")
        print("使用记录：/skill-uses [技能名|任务ID] 核对实际加载版本与任务状态；/extract_now [提示] 提交显式或连续窗口并等待草稿，候选不自动采纳。")
        print("会话记忆：/session-memory show|save|compact|off|use [记录ID]|clear；具名会话的 /compact、自动及工具折叠共用独立记忆；停用后暂停这些折叠。")
        if session_memory_record is not None:
            print(f"已恢复独立会话记忆：{session_memory_record['id']}；仅供参考，历史工具不会重放。")

    turn_start = len(messages)

    def save_progress(history: list[dict]) -> None:
        if session is not None:
            try:
                snapshot = plan.snapshot() if session.with_tools else None
                session.save(history, plan=snapshot)
                if single_run is not None:
                    single_run.session["saved"] = True
            except (ValueError, OSError, RuntimeError) as error:
                if single_run is not None:
                    single_run.session["saved"] = False
                    single_run.fail(error, phase="session_save")
                print(f"会话保存失败：{error} 本轮仍在内存中，磁盘存档未更新。", file=sys.stderr)

    def confirm_with_checkpoint(name: str, arguments: dict, reason: str) -> bool:
        if single_run is not None:
            print(f"单次模式不读取审批输入，拒绝本次确认：{name}。", file=sys.stderr)
            return False
        if name == "exit_plan_mode" and session is not None:
            save_progress(messages[:turn_start])
        return confirm_in_terminal(name, arguments, reason)

    def execute_with_permissions(name: str, arguments: dict, *, allow_skill_fork: bool = True) -> str:
        # 前台模型请求前已结算后台；并发只读工具仅检查预算，不回收 Future 或写候选。
        usage_stats.check_budget(prices, max_cost)
        result = execute_tool(
            name, arguments, allow=allow, deny=deny, confirm=confirm_with_checkpoint,
            plan=plan, request_compaction=queue_compaction, delegate=delegate_agent, agent_types=list(custom_agents),
            fork_skill=delegate_skill if allow_skill_fork else None,
            mcp=mcp,
            search_tools=search_mcp_tools if tool_catalog is not None else None,
            exposed_mcp_tools=exposed_mcp_tools,
        )
        # 并发只读工具不改变 Plan；快照写入只留在主线程的状态/写入操作之后。
        if name not in READ_TOOLS and session is not None and session.with_tools and plan.plan_file is not None:
            save_progress(messages[:turn_start])
        return result

    def execute_subagent_tool(name: str, arguments: dict) -> str:
        if name == "tool_search" or (mcp is not None and name in mcp.names):
            return f"{TOOL_ERROR_PREFIX}当前子任务能力范围未开放 MCP 工具。"
        return execute_with_permissions(name, arguments, allow_skill_fork=False)

    def delegate_skill(prepared: PreparedSkillCall) -> str:
        print(f"正在执行 fork 技能：{prepared.skill.name}……")
        result = run_fork_skill(
            prepared, lambda history, definitions: call_model_with_context(history, definitions, for_subagent=True),
            execute_subagent_tool, max_turns=max_turns,
        )
        print(f"fork 技能 {prepared.skill.name} 已返回结果。")
        return result.text

    def delegate_agent(prompt: str, agent_type: str) -> str:
        print(f"正在执行 {agent_type} 子任务……")
        model = lambda history, definitions: call_model_with_context(history, definitions, for_subagent=True)
        try:
            if agent_type in BUILTIN_AGENT_TYPES:
                runner = {"explore": run_explore_agent, "plan": run_plan_agent, "general": run_general_agent}[agent_type]
                result = runner(prompt, model, execute_subagent_tool, max_turns=max_turns)
            else:
                definition = custom_agents.get(agent_type)
                if definition is None:
                    return f"{TOOL_ERROR_PREFIX}本轮没有可用的自定义 Agent：{agent_type}。"
                result = run_custom_agent(prompt, definition, model, execute_subagent_tool, max_turns=max_turns)
        except PlanFormatError as error:
            return f"{TOOL_ERROR_PREFIX}plan 子任务输出格式无效：{error}"
        print(f"{agent_type} 子任务已返回结果。")
        return result.text

    def queue_compaction() -> str:
        nonlocal compaction_requested
        if memory_compaction_paused():
            return f"{TOOL_ERROR_PREFIX}独立会话记忆已停用，未登记折叠；需用户显式 /session-memory use 或 compact。"
        if compaction_requested:
            return "本轮已经登记折叠请求，无需重复调用；实际折叠会在本轮成功结束后尝试。"
        compaction_requested = True
        return "已登记折叠请求；本轮成功结束后尝试整理旧轮次，当前任务完整保留，预算或校验失败时不折叠。"

    def search_mcp_tools(query: str, limit: int) -> str:
        return tool_catalog.search(query, limit, excluded_names=set(deny or []))

    def request_model_with_context(messages: list[dict], definitions: list[dict], on_usage: UsageCallback) -> dict:
        nonlocal exposed_mcp_tools
        if not text_mode:
            definitions = with_custom_agent_definitions(definitions, custom_agents)
            if tool_catalog is not None:
                active_definitions = tool_catalog.definitions()
                exposed_mcp_tools = {tool["name"] for tool in active_definitions}
                definitions = [*definitions, *active_definitions]
            else:
                definitions = [tool for tool in definitions if tool["name"] != "tool_search"]
        messages = with_session_memory(messages, session_memory_record)
        if selected_memory is not None:
            messages = with_memory_context(messages, selected_memory)
        messages = with_skill_context(messages, skill_context)
        if not text_mode:
            if session is not None:
                context = f"当前权限模式：{plan.mode}。恢复的工具消息仅为历史，不要重放旧调用；历史批准不代表新操作已获授权。"
                messages = [{"role": "system", "content": context}, *messages]
            if plan.plan_file is not None:
                messages = [{"role": "system", "content": plan.describe()}, *messages]
        if stream:
            started = False

            def show_text(text: str) -> None:
                nonlocal started
                if not started:
                    print("Personal Agent > ", end="", flush=True)
                    started = True
                print(text, end="", flush=True)

            try:
                if text_mode:
                    message = stream_model_message(messages, on_text=show_text, on_usage=on_usage)
                    validate_text_messages([messages[-1], message])
                else:
                    message = stream_model_message(messages, definitions, on_text=show_text, on_usage=on_usage)
            finally:
                if started:
                    print(flush=True)
            for call in message.get("tool_calls", []):
                print(f"模型请求工具：{call['function']['name']}")
            return message
        if text_mode:
            # 不发送工具定义，并在交给循环前拒绝非文本响应。
            message = get_model_message(messages, on_usage=on_usage)
            validate_text_messages([messages[-1], message])
            return message
        return _call_model_with_progress(messages, definitions, on_usage=on_usage)

    def call_model_with_context(
        messages: list[dict], definitions: list[dict], *, for_compaction: bool = False, for_subagent: bool = False,
        for_skill_draft: bool = False,
        for_skill_eval: bool = False, for_skill_judge: bool = False, for_skill_task_eval: bool = False, on_usage_observer: UsageCallback | None = None,
    ) -> dict:
        # 拦截发生在 finally 计数之外，未发出的请求不计为缺失用量。
        settle_model_budget()
        reported = None

        def capture_usage(usage: dict | None) -> None:
            nonlocal reported
            reported = usage

        try:
            if for_skill_task_eval:
                print("正在回放文件工具任务……")
                return get_model_message(messages, definitions, on_usage=capture_usage, max_tokens=evaluation_settings["max_tokens"])
            if for_compaction or for_skill_draft or for_skill_eval or for_skill_judge:
                print("正在评审回放回答……" if for_skill_judge else "正在回放技能方法……" if for_skill_eval else "正在生成技能候选草稿……" if for_skill_draft else "正在生成接续摘要……")
                return get_model_message(messages, on_usage=capture_usage, max_tokens=evaluation_settings["max_tokens"])
            if for_subagent:
                # 子任务使用自己的消息和非流式响应，共用同一预算检查及 finally 用量累计。
                return _call_model_with_progress(messages, definitions, on_usage=capture_usage)
            return request_model_with_context(messages, definitions, capture_usage)
        finally:
            # 失败的对话可以回滚，但已收到的用量不能随之抹掉。
            usage_stats.record(reported)
            if on_usage_observer is not None:
                on_usage_observer(reported)

    def memory_compaction_paused() -> bool:
        return session is not None and session.session_memory is not None and not session.session_memory["enabled"]

    def publish_session_memory(*, compact: bool) -> bool:
        """三种折叠入口与显式 save/compact 共用；引用发布成功后才切换运行状态。"""
        nonlocal turn_start, session_memory_record
        reference = session.session_memory
        previous = session_memory_store.read(reference) if reference is not None else None
        generated = generate_memory(messages, lambda request: call_model_with_context(request, [], for_compaction=True),
                                    previous=previous, compact=compact)
        if generated is None:
            return False
        memory, history, source_count = generated
        reference = session_memory_store.save(memory, history, source_count, compact=compact,
                                              previous_id=previous["id"] if previous else None)
        record = session_memory_store.read(reference)
        candidate = compacted_history(history, record, previous=session_memory_record) if compact else history
        snapshot = plan.snapshot() if session.with_tools else None
        before = context_chars(with_session_memory(messages, session_memory_record))
        session.save(candidate, plan=snapshot, session_memory=reference)
        messages[:] = candidate
        turn_start = len(messages)
        session_memory_record = record
        if compact:
            after = context_chars(with_session_memory(messages, record))
            print(f"上下文已折叠：JSON 字符数（含独立记忆参考）{before} → {after}；最近一轮保留。")
        print(f"会话记忆已保存：{reference['id']}；启用；未执行记忆中的工具或计划。")
        print(f"来源快照：{record['source_archive']}")
        return True

    def perform_compaction() -> bool:
        """具名会话使用独立记忆；无具名聊天保留原六字段摘要。"""
        nonlocal turn_start
        if session is not None:
            if memory_compaction_paused():
                raise ValueError("独立会话记忆已停用；先 /session-memory use，或显式 /session-memory compact。")
            return publish_session_memory(compact=True)
        result = compact_context(
            messages, lambda request: call_model_with_context(request, [], for_compaction=True),
        )
        if result is None:
            return False
        candidate, archive = result
        before, after = context_chars(messages), context_chars(candidate)
        messages[:] = candidate
        turn_start = len(messages)
        print(f"上下文已折叠：JSON 字符数 {before} → {after}；最近一轮保留。")
        print(f"原历史备份：{archive}")
        return True

    jobs = SkillDraftJobs(
        lambda request, observer: get_model_message(request, on_usage=observer, max_tokens=evaluation_settings["max_tokens"]),
        usage_stats.record, lambda: usage_stats.check_budget(prices, max_cost),
        model_settings=evaluation_settings,
    )
    resources.callback(jobs.close)
    learning = SkillLearning(jobs, session_name=session.name if session else None)
    resources.enter_context(skill_learning_scope(learning))

    def finish_skill_task(status, answer=None):
        try:
            learning.finish(status, messages, answer=answer, allowed=plan.state == "executing")
            if learning.pending is not None:
                print(f"连续学习窗口已准备：{learning.pending['skill_name']}。下一条普通输入同时作为反馈和正常任务；/extract_now 可立即提炼。")
        except (ValueError, OSError, RuntimeError) as error:
            print(f"Skill 任务关联或连续窗口未保存：{error}；本轮实际结果保留。", file=sys.stderr)

    def settle_model_budget():
        completed = jobs.poll(wait=True)
        if completed is not None:
            print(format_skill_job(completed))
        usage_stats.check_budget(prices, max_cost)

    if session is not None and session.with_tools and not resume:
        save_progress(messages)
    while single_run is None or not single_run.consumed:
        if termination_requested():
            break
        try:
            completed = jobs.poll()
            if completed is not None:
                print(format_skill_job(completed))
        except (ValueError, OSError, RuntimeError) as error:
            print(f"后台任务收尾失败：{error}", file=sys.stderr)
        try:
            if single_run is None:
                user_input = input("你 > ").strip()
            else:
                single_run.consumed = True
                user_input = single_run.prompt.strip()
        except (EOFError, KeyboardInterrupt):
            if session is not None and session.with_tools:
                save_progress(messages)
            print("\nPersonal Agent 已退出。")
            break

        if not user_input:
            continue

        command = user_input.split(maxsplit=1)[0].lower() if single_run is None else ""

        if command == "/session-memory":
            parts = user_input.split()
            action = parts[1].lower() if len(parts) > 1 else "show"
            if (action not in {"show", "save", "compact", "off", "use", "clear"}
                    or len(parts) > (3 if action == "use" else 2)):
                print("用法：/session-memory show|save|compact|off|use [记录ID]|clear。")
                continue
            if session is None:
                print("独立 session-memory 需要 --session NAME 或 --resume NAME。", file=sys.stderr)
                continue
            try:
                reference = session.session_memory
                if action == "show":
                    if reference is None:
                        print("当前会话没有绑定 session-memory；save 保留历史，compact 折叠旧轮次。")
                    else:
                        record = session_memory_store.read(reference)
                        print(f"独立会话记忆：{reference['id']}；{'启用' if reference['enabled'] else '停用'}。")
                        print(json.dumps(record["memory"], ensure_ascii=False, indent=2))
                    continue
                candidate, record = messages, None
                if action in {"save", "compact"}:
                    try:
                        if not publish_session_memory(compact=action == "compact"):
                            print("没有新增的已完成旧轮次可折叠；原历史和记忆保持不变。")
                    finally:
                        if action == "compact":
                            auto_compact.remember(messages)
                    continue
                elif action == "clear":
                    reference = None
                elif action == "off":
                    if reference is None:
                        raise ValueError("当前没有绑定 session-memory。")
                    reference = {**reference, "enabled": False}
                elif action == "use":
                    reference = session_memory_store.reference(parts[2]) if len(parts) == 3 else ({**reference, "enabled": True} if reference else None)
                    record = session_memory_store.read(reference)
                snapshot = plan.snapshot() if session.with_tools else None
                # 不可变记录可先写入；只有会话引用成功替换后，才改变运行中的历史/上下文。
                session.save(candidate, plan=snapshot, session_memory=reference)
                messages[:] = candidate
                turn_start = len(messages)
                session_memory_record = record if reference is not None and reference["enabled"] else None
                print("独立会话记忆已解除绑定；历史记录保留。" if reference is None else
                      f"会话记忆已保存：{reference['id']}；{'启用' if reference['enabled'] else '停用'}；未执行记忆中的工具或计划。")
            except KeyboardInterrupt:
                print("\n会话记忆操作已中断；请查看绑定和历史，已落盘的记录保留。")
            except MODEL_API_ERRORS as error:
                print(f"会话记忆模型请求失败（{type(error).__name__}），当前绑定和历史未改变。", file=sys.stderr)
            except (ValueError, OSError, RuntimeError) as error:
                print(f"会话记忆操作失败：{error}", file=sys.stderr)
            continue

        if single_run is None and user_input.lower() == "exit":
            if session is not None and session.with_tools:
                save_progress(messages)
            print("Personal Agent 已退出。")
            break

        if command == "/plan":
            parts = user_input.lower().split()
            if len(parts) > 2 or (len(parts) == 2 and parts[1] not in {"status", "submit"}):
                print("用法：/plan 切换到规划或提交审批；/plan status 查看；/plan submit 提交实际计划。")
            elif parts == ["/plan", "status"]:
                print(plan.describe())
            elif text_mode:
                print("Plan 切换需要工具聊天；具名会话请用 --session NAME --with-tools，流式临时聊天请创建工具会话。", file=sys.stderr)
            else:
                # 本地命令之间没有未完成的新轮次，审批检查点须保存全部已完成历史。
                turn_start = len(messages)
                tool = "exit_plan_mode" if plan.state != "executing" or len(parts) == 2 else "enter_plan_mode"
                try:
                    print(execute_tool(tool, {}, plan=plan, allow=allow, deny=deny, confirm=confirm_with_checkpoint))
                    save_progress(messages)
                except KeyboardInterrupt:
                    print("\n计划操作已中断；可用 /plan status 核对当前状态。")
                    save_progress(messages)
                except (ValueError, OSError, RuntimeError) as error:
                    print(f"计划操作失败：{error}", file=sys.stderr)
            continue

        if single_run is None and user_input.lower() == "/usage":
            completed = jobs.poll()
            if completed is not None:
                print(format_skill_job(completed))
            print(usage_stats.format_summary())
            continue

        if single_run is None and user_input.lower() == "/cost":
            completed = jobs.poll()
            if completed is not None:
                print(format_skill_job(completed))
            print(usage_stats.format_cost_summary(prices, max_cost))
            continue

        if command == "/extract_now":
            parts = user_input.split(maxsplit=1)
            try:
                if jobs.future is not None:
                    raise ValueError("已有后台请求尚未结算，请先 /skill-jobs wait 查看结果。")
                if jobs.window is None:
                    if learning.pending is not None:
                        learning.arm_pending(allowed=plan.state == "executing")
                    else:
                        raise ValueError("没有可提交的反馈窗口，请先 /skill-learn 技能名 开启一次性窗口，或 /skill-learn auto 连续学习。")
                job = jobs.extract_now(parts[1] if len(parts) == 2 else "")
                print(format_skill_job(job))
                print("立即提炼已结算；新增/合并候选仍待审阅，未自动采纳。" if job["status"] == "completed" else "立即提炼未生成新的待采纳候选；请查看任务状态和错误。")
            except KeyboardInterrupt:
                print("\n立即提炼已中断；在途请求按原规则取消并结算，请核对 /skill-jobs 与 /skill-candidates；已保存候选不会撤回。")
            except (ValueError, OSError, RuntimeError) as error:
                print(f"立即提炼失败：{error}", file=sys.stderr)
            continue

        if command in {"/skill-learn", "/skill-jobs"}:
            parts = user_input.split(maxsplit=2)
            try:
                if parts[0].lower() == "/skill-learn" and len(parts) <= 3:
                    if len(parts) == 1:
                        print(json.dumps(learning.describe(), ensure_ascii=False))
                    elif parts[1].lower() == "auto":
                        if plan.state != "executing":
                            raise ValueError("Plan 期间不能开启连续学习。")
                        learning.enable(parts[2] if len(parts) == 3 else None)
                        print("连续学习已开启：只绑定后续实际加载的项目 Skill；下一条普通输入照常聊天，额外反馈决策共享预算，候选仍待显式采纳。")
                    elif parts[1].lower() == "off" and len(parts) == 2:
                        learning.disable()
                        jobs.window = None
                        print("反馈窗口和连续学习已关闭；运行中的任务可用 /skill-jobs cancel 任务ID 取消。")
                    elif len(parts) == 2:
                        if learning.enabled:
                            raise ValueError("连续模式已开启；先 /skill-learn off，再开启一次性显式窗口。")
                        prepared = jobs.arm(parts[1], messages, session_name=session.name if session else None)
                        jobs.window = learning.bind_window(prepared)
                        print(f"一次性反馈窗口已开启：{prepared['skill_name']}；已冻结最近任务和版本 {prepared['base_sha256']}。下一条普通输入只作为反馈，不发送为聊天任务。")
                    else:
                        raise ValueError("用法：/skill-learn [auto [技能名]|技能名|off]。")
                elif parts[0].lower() == "/skill-jobs" and len(parts) == 3 and parts[1].lower() == "cancel":
                    print(format_skill_job(jobs.cancel(parts[2])))
                elif parts[0].lower() == "/skill-jobs" and len(parts) <= 2:
                    completed = jobs.poll(wait=len(parts) == 2 and parts[1].lower() == "wait")
                    if completed is not None:
                        print(format_skill_job(completed))
                    if len(parts) == 2 and parts[1].lower() != "wait":
                        print(format_skill_job(jobs.load(parts[1])))
                    else:
                        records = jobs.list_jobs()
                        print("后台草稿记录：" if records else "暂无后台草稿记录。")
                        for record in records:
                            print(format_skill_job(record))
                else:
                    print("用法：/skill-learn [auto [技能名]|技能名|off]；/skill-jobs [wait|任务ID|cancel 任务ID]。")
            except KeyboardInterrupt:
                print("\n已请求取消后台任务；已发出请求结束后仍会结算用量。")
            except (ValueError, OSError, RuntimeError) as error:
                print(f"后台反馈操作失败：{error}", file=sys.stderr)
            continue

        if command == "/skill-uses":
            parts = user_input.split(maxsplit=1)
            try:
                rows = list_skill_tasks(lifecycle, parts[1] if len(parts) == 2 else None)
                feedback = evolution.list_records("feedback")
                for row in rows:
                    row["feedback_ids"] = [item["id"] for item in feedback if item.get("task_use", {}).get("id") == row["id"]]
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            except (ValueError, OSError, RuntimeError) as error:
                print(f"Skill 任务记录读取失败：{error}", file=sys.stderr)
            continue

        if jobs.window is not None and not user_input.startswith("/"):
            try:
                job = jobs.submit_feedback(user_input)
                print(format_skill_job(job))
                print("反馈已保存并提交后台决策；可继续输入命令。新增/合并候选待审阅，不自动采纳。")
            except (ValueError, OSError, RuntimeError) as error:
                print(f"反馈提交失败：{error}", file=sys.stderr)
            continue

        if command == "/mcp":
            if user_input.lower() != "/mcp":
                print("用法：/mcp，列出本次连接的 MCP 工具，不接受额外参数。")
            elif mcp is None:
                print("本次未连接 MCP；启动时使用 --mcp-config PATH 显式配置。")
            else:
                print("MCP 工具（列表不激活）：\n" + "\n".join(f"- {tool['name']} [{'已激活' if tool['name'] in tool_catalog.active_names else '待检索'}]：{tool['description']}" for tool in mcp.definitions()))
            continue

        if command == "/compact":
            if user_input.lower() != "/compact":
                print("用法：/compact，不接受额外参数。")
                continue
            try:
                if not perform_compaction():
                    print("暂无可折叠的旧轮次；最近一轮完整保留。")
            except KeyboardInterrupt:
                print("\n折叠已取消，原上下文保留。")
            except MODEL_API_ERRORS as error:
                print(f"折叠请求失败（{type(error).__name__}），原上下文保留。", file=sys.stderr)
            except (ValueError, OSError, RuntimeError) as error:
                print(f"折叠失败：{error} 原上下文保留。", file=sys.stderr)
            finally:
                auto_compact.remember(messages)
            continue

        if command == "/memory":
            parts = user_input.split(maxsplit=3)
            try:
                if len(parts) == 1:
                    entries = list_memories()
                    print("项目记忆：\n" + "\n".join(f"- {entry['name']} [{entry['type']}] 更新于 {entry['updated_at']}（UTC）" for entry in entries) if entries else "当前项目暂无长期记忆。")
                    print(f"当前选用：{selected_memory['name']}。" if selected_memory else "当前未选用记忆。")
                    if selected_memory is not None:
                        print(format_memory_freshness(selected_memory))
                    print("记忆模式：自动，每轮重新匹配。" if auto_memory else "记忆模式：手动。")
                elif len(parts) == 4 and parts[1].lower() == "save":
                    memory_index_cache.clear()
                    save_memory(parts[2], parts[3])
                    print(f"已保存项目记忆：{parts[2]}。")
                elif len(parts) == 3 and parts[1].lower() == "read":
                    entry = read_memory(parts[2])
                    print(f"项目记忆：{entry['name']} [{entry['type']}]\n{format_memory_freshness(entry)}\n{entry['content']}")
                elif len(parts) == 3 and parts[1].lower() == "use":
                    selected_memory = read_memory(parts[2])
                    auto_memory = False
                    memory_index_cache.clear()
                    print(f"已选用项目记忆：{selected_memory['name']}；/memory off 停止使用。")
                    print(format_memory_freshness(selected_memory))
                elif len(parts) == 2 and parts[1].lower() == "auto":
                    selected_memory = None
                    auto_memory = True
                    memory_index_cache.clear()
                    print("已开启自动记忆选择，每轮最多匹配一条；/memory off 关闭。")
                    try:
                        memory_index_cache.load()
                    except (ValueError, OSError) as error:
                        print(f"记忆索引预取失败：{error} 后续提问时会重试加载。", file=sys.stderr)
                elif len(parts) == 2 and parts[1].lower() == "off":
                    selected_memory = None
                    auto_memory = False
                    memory_index_cache.clear()
                    print("已关闭记忆选用及自动选择；已有聊天历史保留。")
                elif len(parts) == 2 and parts[1].lower() == "index":
                    memory_index_cache.clear()
                    rebuild_memory_index()
                    print(load_memory_index())
                else:
                    print("用法：/memory；/memory save 名称 正文；/memory read 名称；/memory use 名称；/memory auto；/memory off；/memory index。")
            except (ValueError, OSError) as error:
                print(f"记忆操作失败：{error}", file=sys.stderr)
            continue

        if single_run is None and user_input.lower() == "/clear":
            # 先保存空历史，成功后再清内存，避免磁盘失败时丢失当前上下文。
            try:
                if session is not None:
                    snapshot = plan.snapshot() if session.with_tools else None
                    session.save([], plan=snapshot, session_memory=None)
            except (ValueError, OSError, RuntimeError) as error:
                print(f"清空失败：{error} 当前会话历史保留。", file=sys.stderr)
                continue
            try:
                jobs.reset()
            except (ValueError, OSError) as error:
                print(f"反馈窗口已关闭，取消回执未保存（{type(error).__name__}）；已发出请求仍会结算。", file=sys.stderr)
            learning.reset()
            messages.clear()
            session_memory_record = None
            turn_start = 0
            selected_memory = None
            auto_memory = False
            memory_index_cache.clear()
            auto_compact.reset()
            skill_context = ""
            if tool_catalog is not None:
                tool_catalog.reset()
            exposed_mcp_tools = set()
            print("当前会话历史已清空，记忆选用及自动选择已关闭；本次用量、预算和 Plan 状态保留。MCP 工具加载状态已重置。")
            continue

        if command in {"/skill-stats", "/skill-archive", "/skill-archives", "/skill-restore"}:
            parts = user_input.split(maxsplit=2)
            try:
                if command == "/skill-stats" and len(parts) <= 2:
                    print(format_skill_statistics(lifecycle.statistics(parts[1] if len(parts) == 2 else None)))
                elif command == "/skill-archive" and len(parts) >= 2:
                    record = lifecycle.archive(parts[1], parts[2] if len(parts) == 3 else "")
                    print(f"项目技能已归档：{record['name']}；{record['id']}；原目录：{record['skill_path']}。")
                elif command == "/skill-archives" and len(parts) == 1:
                    records = lifecycle.list_archives()
                    print("技能归档记录：" if records else "暂无技能归档记录。")
                    for record in records:
                        print(f"- {record['id']} [{record['status']}] {record['name']}；原路径：{record['skill_path']}；原因：{record['reason'] or '未填写'}")
                elif command == "/skill-restore" and len(parts) == 2:
                    record = lifecycle.restore(parts[1])
                    print(f"项目技能已恢复（重复命令不重复移动）：{record['name']}；{record['skill_path']}。")
                else:
                    print("用法：/skill-stats [技能名]；/skill-archive 技能名 [原因]；/skill-archives；/skill-restore 归档ID。")
            except KeyboardInterrupt:
                print("\n技能生命周期操作已中断，请核对归档目录和原技能位置。")
            except (ValueError, OSError, RuntimeError) as error:
                print(f"技能生命周期操作失败：{error}", file=sys.stderr)
            continue

        if command in {"/skill-eval", "/skill-eval-judge", "/skill-eval-tools", "/skill-evals", "/skill-champions"}:
            parts = user_input.split(maxsplit=2)
            try:
                if command == "/skill-eval-tools" and len(parts) == 3:
                    report = evaluator.evaluate_tools(
                        parts[1], parts[2],
                        lambda request, definitions, observer: call_model_with_context(request, definitions, for_skill_task_eval=True, on_usage_observer=observer),
                        model_settings=evaluation_settings,
                        permission_mode=plan.mode, allow=allow, deny=deny, confirm=confirm_with_checkpoint, max_turns=max_turns,
                        prices=prices, check_budget=lambda: usage_stats.check_budget(prices, max_cost),
                    )
                    print(format_evaluation(report))
                    print(f"报告：{evaluator._path(report['id'])}")
                elif command in {"/skill-eval", "/skill-eval-judge"} and len(parts) == 3:
                    report = evaluator.evaluate(
                        parts[1], parts[2],
                        lambda request, observer: call_model_with_context(request, [], for_skill_eval=True, on_usage_observer=observer),
                        model_settings=evaluation_settings,
                        prices=prices,
                        judge_model=(lambda request, observer: call_model_with_context(request, [], for_skill_judge=True, on_usage_observer=observer)) if command == "/skill-eval-judge" else None,
                    )
                    print(format_evaluation(report))
                    print(f"报告：{evaluator._path(report['id'])}")
                    if report.get("selection", {}).get("champion_eligible"):
                        print(f"champion 方法留档：{evaluator._champion_path(report['id'])}")
                elif command == "/skill-evals" and len(parts) == 1:
                    reports = evaluator.list_reports()
                    print("技能评估记录：" if reports else "暂无技能评估记录。")
                    for report in reports:
                        print(f"- {report['id']} [{report['status']}] {report['candidate_id']}：{report['decision']}")
                elif command == "/skill-evals" and len(parts) == 2:
                    print(format_evaluation(evaluator.load(parts[1]), detail=True))
                elif command == "/skill-champions" and len(parts) in {1, 2}:
                    records = evaluator.list_champions() if len(parts) == 1 else [evaluator.champion(parts[1])]
                    print("champion 方法留档：" if records else "暂无 champion 方法留档。")
                    for record in records:
                        print(f"- {record['id']}：{record['skill_name']} / {record['candidate_id']}；方法 SHA256={record['method']['sha256']}；未自动部署。")
                        if len(parts) == 2:
                            print(record["method"]["text"])
                else:
                    print("用法：/skill-eval、/skill-eval-judge 或 /skill-eval-tools 候选ID 样本JSON路径；/skill-evals 或 /skill-champions [评估ID]。")
            except KeyboardInterrupt:
                print("\n技能回放已取消。")
            except (ValueError, OSError, RuntimeError) as error:
                print(f"技能回放失败：{error}", file=sys.stderr)
            continue

        if command in {"/feedback", "/skill-feedback", "/skill-draft", "/skill-decisions", "/skill-candidates", "/skill-adopt", "/skill-reject"}:
            parts = user_input.split(maxsplit=2)
            try:
                if command == "/feedback" and len(parts) == 1:
                    records = evolution.list_records("feedback")
                    print("任务反馈：" if records else "暂无任务反馈；完成问答后使用 /feedback 技能名 反馈内容。")
                    for record in records:
                        rating = f" [评分={record['rating']}]" if "rating" in record else ""
                        print(f"- {record['id']} → {record['skill_name']}{rating}：{record['feedback'][:80]}")
                elif command == "/feedback" and len(parts) == 3:
                    record = evolution.record_feedback(parts[1], parts[2], messages, session_name=session.name if session else None)
                    print(f"已记录最近一轮任务的反馈：{record['id']}；/skill-draft {record['id']} 提炼并生成决策。")
                elif command == "/skill-feedback":
                    rated_parts = user_input.split(maxsplit=3)
                    if len(rated_parts) < 3:
                        print("用法：/skill-feedback 技能名 评分 [说明]；评分作为原始标签保存，不自动生成草稿。")
                    else:
                        note = rated_parts[3] if len(rated_parts) == 4 else f"用户评分：{rated_parts[2]}；未提供补充说明。"
                        record = evolution.record_feedback(rated_parts[1], note, messages, session_name=session.name if session else None, rating=rated_parts[2])
                        print(f"已记录最近一轮任务的评分反馈：{record['id']}；评分={record['rating']}；未请求模型，未修改活动技能。/skill-draft {record['id']} 可基于此反馈生成候选。")
                elif command == "/skill-draft" and len(parts) == 2:
                    candidate = evolution.draft(parts[1], lambda request: call_model_with_context(request, [], for_skill_draft=True), model_name=model_settings["model"])
                    if candidate["kind"] == "decision":
                        print(format_decision(candidate))
                    else:
                        print(evolution.describe(candidate["id"]))
                        if candidate["status"] == "pending":
                            print(f"草稿已保存，尚未采纳；/skill-adopt {candidate['id']} 采纳，或 /skill-reject {candidate['id']} 拒绝。")
                        else:
                            print(f"返回既有候选 {candidate['id']}，当前状态 {candidate['status']}；重复提案不重置状态。")
                elif command == "/skill-decisions" and len(parts) <= 2:
                    records = evolution.list_records("decision") if len(parts) == 1 else [evolution._read("decision", parts[1])]
                    print("技能决策记录：" if records else "暂无三类决策记录；旧两字段草稿仍可作为 merge 候选使用。")
                    for record in records:
                        print(format_decision(record))
                elif command == "/skill-candidates" and len(parts) == 1:
                    records = evolution.list_records("candidate")
                    print("技能候选：" if records else "暂无技能候选；先记录反馈，再 /skill-draft 反馈ID。")
                    for record in records:
                        print(f"- {record['id']} [{record['status']}] [{record.get('action', 'merge')}] {record['skill_name']}：{record['lesson'][:80]}")
                elif command == "/skill-candidates" and len(parts) == 2:
                    print(evolution.describe(parts[1]))
                elif command == "/skill-adopt" and len(parts) == 2:
                    candidate = evolution.adopt(parts[1])
                    if candidate.get("action") == "add":
                        print(f"新增候选已采纳（重复命令不重复创建）：{candidate['id']}；目标：{candidate['skill_path']}；SHA256：{candidate['created_sha256']}")
                    else:
                        print(f"候选已采纳（重复命令不重复追加）：{candidate['id']}；目标：{candidate['skill_path']}\n原版本快照：{candidate['snapshot']}")
                elif command == "/skill-reject" and len(parts) == 2:
                    candidate = evolution.reject(parts[1])
                    print(f"候选已拒绝：{candidate['id']}。")
                else:
                    print("用法：/feedback [技能名 反馈内容]；/skill-draft 反馈ID；/skill-decisions [决策ID]；/skill-candidates [候选ID]；/skill-adopt 候选ID；/skill-reject 候选ID。")
            except KeyboardInterrupt:
                print("\n操作已中断；若正在采纳，请查看活动技能与快照，重试相同候选 ID 可核对采纳标记。")
            except MODEL_API_ERRORS as error:
                print(f"候选模型请求失败（{type(error).__name__}），活动技能未修改。", file=sys.stderr)
            except (ValueError, OSError, RuntimeError) as error:
                print(f"技能反馈操作失败：{error}", file=sys.stderr)
            continue

        if command == "/skill-create":
            parts = user_input.split(maxsplit=1)
            fields = [field.strip() for field in parts[1].split("|", 3)] if len(parts) == 2 else []
            if len(fields) != 4:
                print("用法：/skill-create 名称 | 描述 | 适用场景（可留空） | 正文。")
                continue
            try:
                path = create_project_skill(fields[0], fields[1], fields[3], when_to_use=fields[2])
                print(f"已创建项目技能：{fields[0]}；{path}；输入 /{fields[0]} 参数 可调用。")
            except KeyboardInterrupt:
                print("\n技能创建已取消。")
            except (ValueError, OSError) as error:
                print(f"技能创建失败：{error}", file=sys.stderr)
            continue

        if command == "/skill-evolve":
            parts = user_input.split(maxsplit=2)
            if len(parts) != 3:
                print("用法：/skill-evolve 技能名 可复用经验。")
                continue
            try:
                path, snapshot = evolve_project_skill(parts[1], parts[2])
                print(f"已演化项目技能：{parts[1]}；{path}\n原版本快照：{snapshot}")
            except KeyboardInterrupt:
                print("\n技能演化已取消，请检查技能文件与历史快照确认当前状态。")
            except (ValueError, OSError) as error:
                print(f"技能演化失败：{error}", file=sys.stderr)
            continue

        if command == "/agents":
            if user_input.lower() != "/agents":
                print("用法：/agents，列出自定义 Agent 配置（项目优先），不接受额外参数。")
                continue
            try:
                definitions, warnings = discover_agents()
                for warning in warnings:
                    print(f"Agent 发现提示：{warning}", file=sys.stderr)
                print("自定义 Agent（项目优先，按 name 委派，具体操作仍检查父级权限）：" if definitions else "暂无有效自定义 Agent 配置；支持项目 .personal/agents/名称.md 和用户 ~/.personal/agents/名称.md。")
                for definition in definitions:
                    if definition.allowed_tools is None:
                        tools = "未声明（使用 general 能力上限，仍受父级权限约束）"
                    elif not definition.allowed_tools:
                        tools = "无工具（显式 []）"
                    else:
                        tools = "、".join(definition.allowed_tools)
                    status = "与内置类型同名，内置优先，未启用此配置" if definition.name in BUILTIN_AGENT_TYPES else "可委派"
                    source = "项目" if definition.source == "project" else "用户"
                    print(f"- {definition.name} [{status}] {definition.description or '无描述'} [来源：{source}]\n  声明工具：{tools}\n  {definition.file_path}")
            except (ValueError, OSError, RuntimeError) as error:
                print(f"Agent 发现失败：{error}", file=sys.stderr)
            continue

        if command == "/skills":
            parts = user_input.split(maxsplit=2)
            if len(parts) == 2 and parts[1].lower() == "cache":
                print(current_skill_cache().describe())
                continue
            refresh = len(parts) == 2 and parts[1].lower() == "refresh"
            if len(parts) != 1 and not refresh and not (len(parts) == 3 and parts[1].lower() == "search"):
                print("用法：/skills；/skills search 查询；/skills cache；/skills refresh。")
                continue
            try:
                if refresh:
                    reset_skill_cache()
                    print("Skill 缓存已清除，正在重新发现；词法索引在下次查询时重建。")
                skills, warnings = discover_skills()
                for warning in warnings:
                    print(f"技能发现提示：{warning}", file=sys.stderr)
                if len(parts) == 3:
                    print(format_skill_search(parts[2], skills))
                    continue
                print("可用技能（用户级优先）：" if skills else "暂无技能；可放入项目 .personal/skills 或用户 ~/.personal/skills 的子目录。")
                for skill in skills:
                    if not skill.user_invocable:
                        status = "不可手动调用"
                    elif skill.name.casefold() in BUILTIN_COMMAND_NAMES:
                        status = "与内置命令同名，请用 /skill 路径 调用"
                    elif skill.context == "fork":
                        status = "可手动 fork（需工具聊天）"
                    else:
                        status = "可调用"
                    source = "用户" if skill.source == "user" else "项目"
                    print(f"- /{skill.name} [{status}] {skill.description or '无描述'} [来源：{source}]\n  {skill.file_path}")
            except (ValueError, OSError) as error:
                print(f"技能发现失败：{error}", file=sys.stderr)
            continue

        if not user_input.startswith("/") and learning.pending is not None:
            try:
                completed = jobs.poll(wait=True)
                if completed is not None:
                    print(format_skill_job(completed))
                job = learning.submit_next(user_input, allowed=plan.state == "executing")
                print(format_skill_job(job))
            except KeyboardInterrupt:
                learning.pending = None
                print("\n连续反馈等待已取消，当前输入未提交；在途请求仍按原规则结算。")
                continue
            except (ValueError, OSError, RuntimeError) as error:
                print(f"连续反馈未提交：{error}；当前输入继续作为正常任务。", file=sys.stderr)

        turn_input = user_input
        prepared_skill = None
        if single_run is None and user_input.startswith("/"):
            try:
                if command == "/skill":
                    prepared_skill = prepare_skill_call(user_input)
                else:
                    skills, warnings = discover_skills()
                    for warning in warnings:
                        print(f"技能发现提示：{warning}", file=sys.stderr)
                    prepared_skill = prepare_skill_call(user_input, skills)
                if prepared_skill.skill.context == "fork" and text_mode:
                    raise ValueError("fork 需要工具聊天；新建具名会话时请加 --with-tools。")
                turn_input = prepared_skill.user_message
            except (ValueError, OSError) as error:
                print(f"技能调用失败：{error}", file=sys.stderr)
                continue
            print("技能将作为独立子任务执行。" if prepared_skill.skill.context == "fork" else "技能已展开，将在当前对话中执行。")

        fork_skill = prepared_skill is not None and prepared_skill.skill.context == "fork"
        if not fork_skill and not memory_compaction_paused() and auto_compact.should_compact(messages):
            print("历史达到自动折叠阈值，正在检查旧轮次……")
            try:
                perform_compaction()
            except KeyboardInterrupt:
                if single_run is not None:
                    single_run.fail(KeyboardInterrupt(), cancelled=True)
                print("\n自动折叠已取消，原上下文保留，本次提问未提交。")
                continue
            except MODEL_API_ERRORS as error:
                print(f"自动折叠请求失败（{type(error).__name__}），保留原上下文继续本轮请求。", file=sys.stderr)
            except (ValueError, OSError, RuntimeError) as error:
                print(f"自动折叠失败：{error} 保留原上下文继续本轮请求。", file=sys.stderr)
            finally:
                auto_compact.remember(messages)

        turn_start = len(messages)
        compaction_requested = False
        skill_context = ""
        custom_agents = {}
        learning.begin(turn_input)
        messages.append({"role": "user", "content": turn_input})

        print("正在请求模型……")
        try:
            if not text_mode and not fork_skill:
                settle_model_budget()
                try:
                    definitions, warnings = discover_agents()
                    for warning in warnings:
                        print(f"Agent 发现提示：{warning}", file=sys.stderr)
                    custom_agents = {definition.name: definition for definition in definitions if definition.name not in BUILTIN_AGENT_TYPES}
                except (ValueError, OSError, RuntimeError) as error:
                    print(f"Agent 配置发现失败：{error} 本轮仅提供内置类型。", file=sys.stderr)
            if single_run is not None or not user_input.startswith("/"):
                settle_model_budget()
                try:
                    skills, warnings = discover_skills()
                    for warning in warnings:
                        print(f"技能发现提示：{warning}", file=sys.stderr)
                    hits = retrieve_relevant_skills(user_input, skills)
                    skill_context = format_retrieved_skill_context(hits)
                    if hits:
                        for hit in hits:
                            record_skill_event(hit.skill, "retrieved", "candidate_context")
                        print("本轮相关技能候选：" + "、".join(hit.skill.name for hit in hits) + "。")
                except (ValueError, OSError) as error:
                    skill_context = ""
                    print(f"技能候选检索失败：{error} 本轮不提供候选，继续当前任务。", file=sys.stderr)
            if auto_memory and not fork_skill:
                selected_memory = None
                settle_model_budget()
                try:
                    selected_memory = select_relevant_memory(user_input, index_cache=memory_index_cache)
                except (ValueError, OSError) as error:
                    memory_index_cache.clear()
                    print(f"记忆选择失败：{error} 本轮未加载项目记忆。", file=sys.stderr)
                else:
                    print(f"自动选用项目记忆：{selected_memory['name']}。" if selected_memory else "未匹配到相关项目记忆。")
                    if selected_memory is not None:
                        print(format_memory_freshness(selected_memory))
            settle_model_budget()
            if fork_skill:
                assistant_reply = delegate_skill(prepared_skill)
                messages.append({"role": "assistant", "content": assistant_reply})
            else:
                if prepared_skill is not None:
                    record_skill_event(prepared_skill.skill, "inline_loaded", "manual")
                assistant_reply = run_agent_loop(messages, call_model_with_context, max_turns=max_turns, tool_executor=execute_with_permissions)
        except KeyboardInterrupt as error:
            finish_skill_task("cancelled")
            if single_run is not None:
                single_run.capture(messages[turn_start:])
                single_run.fail(error, cancelled=True)
            del messages[turn_start:]
            if session is not None and session.with_tools:
                save_progress(messages)
            print("\n已中断本轮任务，本轮消息未保存。")
            continue
        except MODEL_API_ERRORS as error:
            finish_skill_task("failed")
            if single_run is not None:
                single_run.capture(messages[turn_start:])
                single_run.fail(error)
            del messages[turn_start:]
            if session is not None and session.with_tools:
                save_progress(messages)
            status = getattr(error, "status_code", None)
            print(
                f"本轮请求失败（{type(error).__name__}，状态码：{status}），本轮消息未保存。",
                file=sys.stderr,
            )
            continue
        except (ValueError, OSError, RuntimeError) as error:
            finish_skill_task("failed")
            if single_run is not None:
                single_run.capture(messages[turn_start:])
                single_run.fail(error)
            del messages[turn_start:]
            if session is not None and session.with_tools:
                save_progress(messages)
            print(f"本轮请求失败：{error} 本轮消息未保存。", file=sys.stderr)
            continue

        if not stream or fork_skill:
            print(f"Personal Agent > {assistant_reply}")
        save_progress(messages)
        finish_skill_task("loop_completed", assistant_reply)
        if single_run is not None:
            single_run.complete(assistant_reply, messages[turn_start:])
        if compaction_requested:
            compaction_requested = False
            try:
                if not perform_compaction():
                    print("工具登记的折叠无需处理：暂无可折叠的旧轮次。")
            except KeyboardInterrupt:
                print("\n工具登记的折叠已取消，本轮任务结果保留。")
            except MODEL_API_ERRORS as error:
                print(f"工具登记的折叠请求失败（{type(error).__name__}），本轮任务结果保留。", file=sys.stderr)
            except (ValueError, OSError, RuntimeError) as error:
                print(f"工具登记的折叠失败：{error} 本轮任务结果保留。", file=sys.stderr)
            finally:
                auto_compact.remember(messages)

    return messages


def main(argv: list[str] | None = None) -> None:
    """解析启动参数后进入命令行交互。"""
    parser = argparse.ArgumentParser(description="Personal Agent：具有工具权限与终端确认的命令行助手。")
    parser.add_argument("--protocol", choices=("openai", "anthropic"), help="本次模型协议；不按模型名猜测协议")
    parser.add_argument("--anthropic-provider", choices=("native", "qwen"), help="Messages 服务配置；qwen 显式使用千问官方端点及 DASHSCOPE_API_KEY")
    parser.add_argument("--model", metavar="NAME", help="本次模型名，覆盖对应协议的环境配置")
    parser.add_argument("--api-base", metavar="URL", help="本次 HTTP(S) API 根地址，覆盖环境配置；不接受内嵌凭据或查询串")
    parser.add_argument("--thinking", nargs="?", const="adaptive", choices=("disabled", "adaptive", "enabled"), help="思考模式；Qwen 配置支持显式 enabled/disabled，Anthropic 裸参数表示 adaptive")
    parser.add_argument("--thinking-budget", type=int, metavar="N", help="enabled 思考预算；项目要求小于输出上限，Qwen Chat 1～32768，Messages 至少 1024；千问网关正文和思考分别计限")
    parser.add_argument("--max-output-tokens", type=int, metavar="N", help="本次模型输出上限，适用于两种协议")
    task_input = parser.add_mutually_exclusive_group()
    task_input.add_argument("--prompt", metavar="TEXT", help="执行一个普通任务后退出；不读取终端审批，不解释 REPL 命令")
    task_input.add_argument("--prompt-file", metavar="PATH", help="从 UTF-8 文件读取单次任务；- 表示读取 stdin 至 EOF")
    parser.add_argument("--output-format", choices=("text", "json"), default=None, help="单次任务最终输出格式（默认 text）；进度写入 stderr")
    parser.add_argument("--max-turns", type=int, default=5, metavar="N", help="父循环及每个子任务各自的逻辑模型调用上限，不含 SDK 内部重试，必须为正整数（默认 5）")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--permission-mode", choices=PERMISSION_MODES, default="default", help="权限模式（默认 default）")
    for flag, mode in (("--plan", "plan"), ("--accept-edits", "acceptEdits"), ("--dont-ask", "dontAsk"), ("--yolo", "bypassPermissions")):
        modes.add_argument(flag, dest="permission_mode", action="store_const", const=mode, help=f"--permission-mode {mode} 的快捷入口")
    parser.add_argument("--allow-tool", action="append", help="显式允许的精确工具名（含已配置 MCP 工具），可重复指定")
    parser.add_argument("--deny-tool", action="append", help="显式禁止的精确工具名（含已配置 MCP 工具），可重复指定")
    parser.add_argument("--mcp-config", metavar="PATH", help="显式加载本地 stdio MCP 配置；启动配置中的程序，默认不连接")
    parser.add_argument("--mcp-eager", action="store_true", help="配合 --mcp-config 全量提供外部工具定义；默认先 tool_search 检索激活")
    session_options = parser.add_mutually_exclusive_group()
    session_options.add_argument("--session", metavar="NAME", help="创建具名普通文本会话，并在每轮成功后保存")
    session_options.add_argument("--resume", metavar="NAME", help="恢复本项目指定会话，自动识别文本/工具类型并继续保存")
    parser.add_argument("--with-tools", action="store_true", help="配合 --session 新建含工具历史的会话；--resume 自动识别")
    parser.add_argument("--stream", action="store_true", help="流式显示正文；工具会话中先完整拼接请求再审批执行")
    parser.add_argument("--input-price", type=parse_token_price, metavar="YUAN", help="输入单价，元/百万 token；须与 --output-price 同时设置")
    parser.add_argument("--output-price", type=parse_token_price, metavar="YUAN", help="输出单价，元/百万 token；须与 --input-price 同时设置")
    parser.add_argument("--max-cost", type=parse_token_price, metavar="YUAN", help="本次运行估算费用上限（元），须配置两种单价；0 禁止模型请求，在途请求可能超额")
    parser.add_argument("--auto-compact-chars", type=int, metavar="N", help="历史 JSON 达到 N 字符时自动折叠旧轮次，须为正整数；默认关闭")
    args = parser.parse_args(argv)
    if args.max_turns < 1:
        parser.error("--max-turns 必须是正整数。")
    if args.max_output_tokens is not None and args.max_output_tokens < 1:
        parser.error("--max-output-tokens 必须是正整数。")
    if args.thinking_budget is not None and args.thinking_budget < 1:
        parser.error("--thinking-budget 必须为正整数。")
    if (args.input_price is None) != (args.output_price is None):
        parser.error("--input-price 和 --output-price 必须同时设置。")
    single = args.prompt is not None or args.prompt_file is not None
    if args.output_format is not None and not single:
        parser.error("--output-format 需要 --prompt 或 --prompt-file。")
    prompt = args.prompt
    if args.prompt_file is not None:
        try:
            prompt = sys.stdin.read() if args.prompt_file == "-" else Path(args.prompt_file).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as error:
            parser.error(f"无法读取 UTF-8 提示文件（{type(error).__name__}）。")
    if single and not prompt.strip():
        parser.error("单次任务提示不能为空。")
    overrides = {key: value for key, value in {
        "protocol": args.protocol, "model": args.model, "base_url": args.api_base, "anthropic_provider": args.anthropic_provider,
        "thinking": args.thinking, "thinking_budget": args.thinking_budget, "max_tokens": args.max_output_tokens,
    }.items() if value is not None}
    with termination_scope() as termination:
        try:
            prices = TokenPrices(args.input_price, args.output_price) if args.input_price is not None else None
            runner = (lambda **settings: run_once(prompt, **settings)) if single else run_repl
            result = runner(
                max_turns=args.max_turns, permission_mode=args.permission_mode,
                allow=args.allow_tool, deny=args.deny_tool,
                session_name=args.resume if args.resume is not None else args.session,
                resume=args.resume is not None,
                with_tools=args.with_tools,
                stream=args.stream,
                prices=prices,
                max_cost=args.max_cost,
                auto_compact_chars=args.auto_compact_chars,
                **({"mcp_config": args.mcp_config} if args.mcp_config is not None else {}),
                **({"mcp_eager": True} if args.mcp_eager else {}),
                **({"model_overrides": overrides} if overrides else {}),
            )
            if single:
                if termination.signum is not None:
                    result["exit_code"] = 128 + termination.signum
                print_report(result, args.output_format or "text")
                if result["exit_code"]:
                    raise SystemExit(result["exit_code"])
        except KeyboardInterrupt:
            if termination.signum is None:
                raise
        except (ValueError, OSError, McpError) as error:
            print(f"启动失败：{error}", file=sys.stderr)
            raise SystemExit(1) from None
        if termination.signum is not None:
            print("收到停止信号，当前 CLI 运行已结束。", file=sys.stderr)
            raise SystemExit(128 + termination.signum)

if __name__ == "__main__":
    main()
