"""交互与单次任务的执行入口；参数解析和本地命令由独立模块处理。"""

import json
import sys
from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path

from .model_errors import MODEL_API_ERRORS
from .cli import parse_args
from .repl_commands import (
    create_skill_from_command, evolve_skill_from_command, run_skill_evaluation_command,
    run_skill_feedback_command, run_skill_lifecycle_command, show_agents, show_skills,
)

from .agent import run_agent_loop
from .agent_config import AgentDefinition, BUILTIN_AGENT_TYPES, discover_agents
from .context import AutoCompactPolicy
from .conversation import ConversationState
from .project_memory import ProjectMemoryState
from .config import get_model_settings, model_config_scope
from .cancellation import termination_requested, termination_scope
from .llm import MAX_REQUEST_RETRIES, get_model_message, stream_model_message
from .memory import select_relevant_memory, with_memory_context
from .permissions import READ_TOOLS, SUPPORTED_TOOLS, confirm_in_terminal
from .mcp_client import McpError
from .mcp_runtime import McpToolRuntime, load_mcp_config
from .plan import PlanSession
from .session import Session, validate_text_messages
from .session_memory import SessionMemoryStore, generate_memory, with_session_memory
from .single_run import SingleRun, print_report, run_once
from .skill_search import (
    format_retrieved_skill_context, retrieve_relevant_skills, with_skill_context,
)
from .skill_evolution import SkillEvolutionStore
from .skill_eval import SkillEvaluator
from .skill_background import SkillDraftJobs, format_skill_job
from .skill_cache import skill_cache_scope
from .skill_lifecycle import SkillLifecycleStore, record_skill_event
from .skill_learning import SkillLearning, list_skill_tasks, skill_learning_scope
from .skills import (
    PreparedSkillCall, discover_skills, prepare_skill_call,
)
from .subagent import PlanFormatError, run_custom_agent, run_explore_agent, run_fork_skill, run_general_agent, run_plan_agent
from .tool_registry import TOOL_ERROR_PREFIX, execute_tool, with_custom_agent_definitions
from .tool_search import DeferredToolCatalog
from .usage import TokenPrices, UsageCallback, UsageStats


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
    memory_store = SessionMemoryStore(session) if session is not None else None
    restored_memory = (memory_store.read(session.session_memory)
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
    conversation = ConversationState(
        messages, session=session, plan=plan, auto_compact=auto_compact,
        memory_store=memory_store, memory_record=restored_memory,
    )
    project_memory = ProjectMemoryState()
    skill_context = ""
    custom_agents: dict[str, AgentDefinition] = {}
    mcp = resources.enter_context(McpToolRuntime(load_mcp_config(mcp_config))) if mcp_config is not None else None
    tool_catalog = DeferredToolCatalog(mcp.definitions(), eager=mcp_eager) if mcp is not None else None
    exposed_mcp_tools: set[str] = set()
    supported = SUPPORTED_TOOLS | (mcp.names if mcp is not None else set())
    for rules in (allow, deny):
        if rules is not None and (not isinstance(rules, list) or any(not isinstance(name, str) or name not in supported for name in rules)):
            raise ValueError("allow/deny 必须使用当前已注册的精确工具名；MCP 名称可通过 /mcp 查看。")
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
        if conversation.memory_record is not None:
            print(f"已恢复独立会话记忆：{conversation.memory_record['id']}；仅供参考，历史工具不会重放。")

    conversation.checkpoint()

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
            save_progress(conversation.completed_history)
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
            save_progress(conversation.completed_history)
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
        if conversation.compaction_paused:
            return f"{TOOL_ERROR_PREFIX}独立会话记忆已停用，未登记折叠；需用户显式 /session-memory use 或 compact。"
        if conversation.compaction_requested:
            return "本轮已经登记折叠请求，无需重复调用；实际折叠会在本轮成功结束后尝试。"
        conversation.compaction_requested = True
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
        messages = with_session_memory(messages, conversation.memory_record)
        if project_memory.selected is not None:
            messages = with_memory_context(messages, project_memory.selected)
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

    def summarize_context(request: list[dict]) -> dict:
        return call_model_with_context(request, [], for_compaction=True)


    def perform_compaction() -> bool:
        return conversation.compact(summarize_context, generate=generate_memory)

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
            conversation.handle_memory_command(user_input, summarize_context, generate=generate_memory)
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
                conversation.checkpoint()
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
                conversation.auto_compact.remember(messages)
            continue

        if command == "/memory":
            project_memory.handle_command(user_input)
            continue

        if single_run is None and user_input.lower() == "/clear":
            # 先保存空历史，成功后再清内存，避免磁盘失败时丢失当前上下文。
            try:
                conversation.save_empty_history()
            except (ValueError, OSError, RuntimeError) as error:
                print(f"清空失败：{error} 当前会话历史保留。", file=sys.stderr)
                continue
            try:
                jobs.reset()
            except (ValueError, OSError) as error:
                print(f"反馈窗口已关闭，取消回执未保存（{type(error).__name__}）；已发出请求仍会结算。", file=sys.stderr)
            learning.reset()
            conversation.clear_history()
            project_memory.reset()
            conversation.auto_compact.reset()
            skill_context = ""
            if tool_catalog is not None:
                tool_catalog.reset()
            exposed_mcp_tools = set()
            print("当前会话历史已清空，记忆选用及自动选择已关闭；本次用量、预算和 Plan 状态保留。MCP 工具加载状态已重置。")
            continue

        if command in {"/skill-stats", "/skill-archive", "/skill-archives", "/skill-restore"}:
            run_skill_lifecycle_command(command, user_input, lifecycle)
            continue

        if command in {"/skill-eval", "/skill-eval-judge", "/skill-eval-tools", "/skill-evals", "/skill-champions"}:
            run_skill_evaluation_command(
                command, user_input, evaluator, call_model=call_model_with_context,
                model_settings=evaluation_settings,
                tool_options={"permission_mode": plan.mode, "allow": allow, "deny": deny,
                    "confirm": confirm_with_checkpoint, "max_turns": max_turns,
                    "prices": prices, "check_budget": lambda: usage_stats.check_budget(prices, max_cost)},
            )
            continue

        if command in {"/feedback", "/skill-feedback", "/skill-draft", "/skill-decisions", "/skill-candidates", "/skill-adopt", "/skill-reject"}:
            run_skill_feedback_command(
                command, user_input, evolution, messages=messages,
                session_name=session.name if session else None,
                draft_model=lambda request: call_model_with_context(request, [], for_skill_draft=True),
                model_name=model_settings["model"],
            )
            continue

        if command == "/skill-create":
            create_skill_from_command(user_input)
            continue

        if command == "/skill-evolve":
            evolve_skill_from_command(user_input)
            continue

        if command == "/agents":
            show_agents(user_input, discover=discover_agents)
            continue

        if command == "/skills":
            show_skills(user_input, discover=discover_skills)
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
        if not fork_skill and not conversation.compaction_paused and conversation.auto_compact.should_compact(messages):
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
                conversation.auto_compact.remember(messages)

        conversation.begin_turn()
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
            if project_memory.automatic and not fork_skill:
                project_memory.select_for_turn(user_input, selector=select_relevant_memory, before_select=settle_model_budget)
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
                single_run.capture(conversation.turn_messages)
                single_run.fail(error, cancelled=True)
            conversation.rollback_turn()
            if session is not None and session.with_tools:
                save_progress(messages)
            print("\n已中断本轮任务，本轮消息未保存。")
            continue
        except MODEL_API_ERRORS as error:
            finish_skill_task("failed")
            if single_run is not None:
                single_run.capture(conversation.turn_messages)
                single_run.fail(error)
            conversation.rollback_turn()
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
                single_run.capture(conversation.turn_messages)
                single_run.fail(error)
            conversation.rollback_turn()
            if session is not None and session.with_tools:
                save_progress(messages)
            print(f"本轮请求失败：{error} 本轮消息未保存。", file=sys.stderr)
            continue

        if not stream or fork_skill:
            print(f"Personal Agent > {assistant_reply}")
        save_progress(messages)
        finish_skill_task("loop_completed", assistant_reply)
        if single_run is not None:
            single_run.complete(assistant_reply, conversation.turn_messages)
        if conversation.compaction_requested:
            conversation.compaction_requested = False
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
                conversation.auto_compact.remember(messages)

    return messages


def main(argv: list[str] | None = None) -> None:
    """解析启动参数后进入命令行交互。"""
    args = parse_args(argv)
    single = args.prompt is not None
    prompt = args.prompt
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
