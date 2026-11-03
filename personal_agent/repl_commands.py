"""本地 Skill 与 Agent 命令处理，与对话执行流程分开。

REPL 提供存储对象、当前历史、发现函数和受预算约束的模型回调。
命令处理函数不独立管理对话状态或授予执行权限。
"""

from collections.abc import Callable
import sys

from .agent_config import BUILTIN_AGENT_TYPES
from .model_errors import MODEL_API_ERRORS
from .skill_cache import current_skill_cache, reset_skill_cache
from .skill_decision import format_decision
from .skill_eval import SkillEvaluator, format_evaluation
from .skill_evolution import SkillEvolutionStore
from .skill_lifecycle import SkillLifecycleStore, format_skill_statistics
from .skill_search import format_skill_search
from .skills import BUILTIN_COMMAND_NAMES, create_project_skill, evolve_project_skill

def run_skill_lifecycle_command(command: str, user_input: str, lifecycle: SkillLifecycleStore) -> None:
    """展示统计，或执行用户明确要求的归档与恢复。"""
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


def run_skill_evaluation_command(
    command: str, user_input: str, evaluator: SkillEvaluator, *,
    call_model: Callable[..., dict], model_settings: dict, tool_options: dict,
) -> None:
    """沿用调用方的模型、权限和预算执行评估。

    tool_options 为 evaluate_tools 的执行参数，其中 prices 也用于文本回放。
    """
    parts = user_input.split(maxsplit=2)
    try:
        if command == "/skill-eval-tools" and len(parts) == 3:
            report = evaluator.evaluate_tools(
                parts[1], parts[2],
                lambda request, definitions, observer: call_model(request, definitions, for_skill_task_eval=True, on_usage_observer=observer),
                model_settings=model_settings,
                **tool_options,
            )
            print(format_evaluation(report))
            print(f"报告：{evaluator._path(report['id'])}")
        elif command in {"/skill-eval", "/skill-eval-judge"} and len(parts) == 3:
            report = evaluator.evaluate(
                parts[1], parts[2],
                lambda request, observer: call_model(request, [], for_skill_eval=True, on_usage_observer=observer),
                model_settings=model_settings,
                prices=tool_options["prices"],
                judge_model=(lambda request, observer: call_model(request, [], for_skill_judge=True, on_usage_observer=observer)) if command == "/skill-eval-judge" else None,
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


def run_skill_feedback_command(
    command: str, user_input: str, evolution: SkillEvolutionStore, *,
    messages: list[dict], session_name: str | None,
    draft_model: Callable[[list[dict]], dict], model_name: str,
) -> None:
    """记录反馈，或按明确命令生成、查看、采纳和拒绝候选。"""
    parts = user_input.split(maxsplit=2)
    try:
        if command == "/feedback" and len(parts) == 1:
            records = evolution.list_records("feedback")
            print("任务反馈：" if records else "暂无任务反馈；完成问答后使用 /feedback 技能名 反馈内容。")
            for record in records:
                rating = f" [评分={record['rating']}]" if "rating" in record else ""
                print(f"- {record['id']} → {record['skill_name']}{rating}：{record['feedback'][:80]}")
        elif command == "/feedback" and len(parts) == 3:
            record = evolution.record_feedback(parts[1], parts[2], messages, session_name=session_name)
            print(f"已记录最近一轮任务的反馈：{record['id']}；/skill-draft {record['id']} 提炼并生成决策。")
        elif command == "/skill-feedback":
            rated_parts = user_input.split(maxsplit=3)
            if len(rated_parts) < 3:
                print("用法：/skill-feedback 技能名 评分 [说明]；评分作为原始标签保存，不自动生成草稿。")
            else:
                note = rated_parts[3] if len(rated_parts) == 4 else f"用户评分：{rated_parts[2]}；未提供补充说明。"
                record = evolution.record_feedback(rated_parts[1], note, messages, session_name=session_name, rating=rated_parts[2])
                print(f"已记录最近一轮任务的评分反馈：{record['id']}；评分={record['rating']}；未请求模型，未修改活动技能。/skill-draft {record['id']} 可基于此反馈生成候选。")
        elif command == "/skill-draft" and len(parts) == 2:
            candidate = evolution.draft(parts[1], draft_model, model_name=model_name)
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


def create_skill_from_command(user_input: str) -> None:
    """解析以竖线分隔的创建命令，调用既有 Skill 写入逻辑。"""
    parts = user_input.split(maxsplit=1)
    fields = [field.strip() for field in parts[1].split("|", 3)] if len(parts) == 2 else []
    if len(fields) != 4:
        print("用法：/skill-create 名称 | 描述 | 适用场景（可留空） | 正文。")
        return
    try:
        path = create_project_skill(fields[0], fields[1], fields[3], when_to_use=fields[2])
        print(f"已创建项目技能：{fields[0]}；{path}；输入 /{fields[0]} 参数 可调用。")
    except KeyboardInterrupt:
        print("\n技能创建已取消。")
    except (ValueError, OSError) as error:
        print(f"技能创建失败：{error}", file=sys.stderr)


def evolve_skill_from_command(user_input: str) -> None:
    """通过既有写入逻辑追加用户明确提供的经验。"""
    parts = user_input.split(maxsplit=2)
    if len(parts) != 3:
        print("用法：/skill-evolve 技能名 可复用经验。")
        return
    try:
        path, snapshot = evolve_project_skill(parts[1], parts[2])
        print(f"已演化项目技能：{parts[1]}；{path}\n原版本快照：{snapshot}")
    except KeyboardInterrupt:
        print("\n技能演化已取消，请检查技能文件与历史快照确认当前状态。")
    except (ValueError, OSError) as error:
        print(f"技能演化失败：{error}", file=sys.stderr)


def show_agents(user_input: str, *, discover: Callable[[], tuple[list, list]]) -> None:
    """展示当前 Agent 发现结果，保留委派状态。"""
    if user_input.lower() != "/agents":
        print("用法：/agents，列出自定义 Agent 配置（项目优先），不接受额外参数。")
        return
    try:
        definitions, warnings = discover()
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


def show_skills(user_input: str, *, discover: Callable[[], tuple[list, list]]) -> None:
    """展示、搜索或刷新当前 Skill 发现缓存。"""
    parts = user_input.split(maxsplit=2)
    if len(parts) == 2 and parts[1].lower() == "cache":
        print(current_skill_cache().describe())
        return
    refresh = len(parts) == 2 and parts[1].lower() == "refresh"
    if len(parts) != 1 and not refresh and not (len(parts) == 3 and parts[1].lower() == "search"):
        print("用法：/skills；/skills search 查询；/skills cache；/skills refresh。")
        return
    try:
        if refresh:
            reset_skill_cache()
            print("Skill 缓存已清除，正在重新发现；词法索引在下次查询时重建。")
        skills, warnings = discover()
        for warning in warnings:
            print(f"技能发现提示：{warning}", file=sys.stderr)
        if len(parts) == 3:
            print(format_skill_search(parts[2], skills))
            return
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
