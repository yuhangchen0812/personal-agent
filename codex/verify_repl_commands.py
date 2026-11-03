"""REPL 同名入口联合演示；默认固定模型，--live 另做实际问答及立即提炼。"""

import argparse
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex.verify_delivery import isolated, message, trace
from personal_agent.config import get_model_settings
from personal_agent.llm import get_model_message
from personal_agent.main import main
from personal_agent.permissions import confirm_in_terminal
from personal_agent.plan import PlanSession
from personal_agent.session import Session
from personal_agent.skill_evolution import DRAFT_PROMPT, SkillEvolutionStore
from personal_agent.skills import create_project_skill
from personal_agent.usage import UsageStats


FIXED_SETTINGS = {"protocol": "openai", "model": "fixed-repl-commands", "base_url": "https://example.invalid"}
HISTORY = [{"role": "user", "content": "SAVED_TASK"}, {"role": "assistant", "content": "SAVED_ANSWER"}]


def invoke(arguments):
    output, errors = io.StringIO(), io.StringIO()
    code = 0
    with redirect_stdout(output), redirect_stderr(errors):
        try:
            main(arguments)
        except SystemExit as error:
            code = error.code
    return code, output.getvalue(), errors.getvalue()


def verify_workflow():
    calls, approvals, checkpoints = [], [], []
    counters = {"plan": 0, "execute": 0, "draft": 0}
    checkpoint_history = []
    main_thread = threading.get_ident()
    with tempfile.TemporaryDirectory(prefix="personal-repl-commands-") as temporary:
        root = Path(temporary).resolve()
        with isolated(root), patch("personal_agent.main.get_model_settings", return_value=FIXED_SETTINGS):
            skill = create_project_skill("explain-flow", "解释有依据的执行流程", "只依据材料解释参数、返回值和调用顺序。")
            original = skill.read_bytes()
            store, plan = SkillEvolutionStore(), PlanSession("acceptEdits")

            def model(messages, definitions=None, *, on_usage, **options):
                background = messages[0]["content"] == DRAFT_PROMPT
                calls.append({"route": "draft" if background else "task", "worker_thread": threading.get_ident() != main_thread})
                on_usage({"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13})
                if background:
                    counters["draft"] += 1
                    assert definitions is None and options["max_tokens"] == 1024
                    data = json.loads(messages[1]["content"])
                    assert data["feedback_record"]["task"].startswith("EXEC_TASK")
                    assert "OLD_COMPLETED_TASK" not in str(messages)
                    lesson = ("记录输入依据、实际操作和校验结果，缺少证据时说明待核实。" if counters["draft"] == 1
                              else "解释执行结果时分别写明参数含义、返回值类型和真实调用顺序。")
                    return {"role": "assistant", "content": json.dumps({"lesson": lesson, "rationale": "用户明确要求提炼可复用方法。"}, ensure_ascii=False)}
                latest = [item["content"] for item in messages if item["role"] == "user"][-1]
                if latest.startswith("PLAN_TASK"):
                    counters["plan"] += 1
                    if counters["plan"] == 1:
                        return message(("write-plan", "write_file", {"file_path": plan.plan_file, "content": "明确批准后创建 playground/result.txt，并报告真实结果。"}),
                                       ("blocked-before-approval", "write_file", {"file_path": "playground/blocked.txt", "content": "禁止提前写入"}))
                    return {"role": "assistant", "content": "实际计划已保存，尚待审批。"}
                if latest.startswith("EXEC_TASK"):
                    counters["execute"] += 1
                    if counters["execute"] == 1:
                        return message(("write-result", "write_file", {"file_path": "playground/result.txt", "content": "APPROVED_RESULT"}))
                    return {"role": "assistant", "content": "已按批准的计划写入实际结果。"}
                return {"role": "assistant", "content": "OLD_COMPLETED_ANSWER"}

            def approve(name, arguments, reason):
                assert name == "exit_plan_mode"
                session = Session("commands")
                assert session.load() == checkpoint_history
                assert session.plan_snapshot["state"] == "awaiting_approval"
                assert arguments["plan"] == (root / plan.plan_file).read_text(encoding="utf-8")
                approvals.append(name)
                checkpoints.append({"completed_messages_preserved": len(checkpoint_history), "state": session.plan_snapshot["state"]})
                return confirm_in_terminal(name, arguments, reason)

            def inputs():
                yield "OLD_COMPLETED_TASK：先完成一个独立任务。"
                yield "/plan status"
                yield "/plan"
                session = Session("commands")
                assert session.load() == [{"role": "user", "content": "OLD_COMPLETED_TASK：先完成一个独立任务。"}, {"role": "assistant", "content": "OLD_COMPLETED_ANSWER"}]
                assert session.plan_snapshot["state"] == "planning"
                yield "/plan invalid"
                yield "/plan"  # 没有实际计划文件，不能仅切回执行。
                assert plan.mode == "plan" and not approvals
                yield "PLAN_TASK：调查并写计划，演示未审批的产物写入被拦截。"
                checkpoint_history[:] = Session("commands").load()
                yield "/plan"
                yield "n"
                assert plan.state == "planning" and plan.mode == "plan"
                assert not (root / "playground/blocked.txt").exists()
                yield "/plan submit"
                yield "y"
                assert plan.state == "executing" and plan.mode == "acceptEdits"
                assert not (root / "playground/result.txt").exists()  # 批准不会自动发任务或执行写入。
                yield "EXEC_TASK：现在执行批准的写入。"
                before = Session("commands").load()
                yield "/skill-feedback explain-flow 2 缺少依据，请列出实际验证步骤。"
                yield "/skill-feedback explain-flow helpful"
                yield "/feedback"
                assert Session("commands").load() == before
                assert len(calls) == 5 and len(store.list_records("candidate")) == 0
                yield "/skill-learn explain-flow"
                yield "/extract_now 优先保留实际验证步骤。"
                assert len(store.list_records("candidate")) == 1
                yield "/skill-learn explain-flow"
                yield "/extract_now"  # 默认中性提示也消费一次窗口。
                assert len(store.list_records("candidate")) == 2
                yield "/extract_now 不应重复发请求。"
                yield "/usage"
                yield "exit"

            with patch("personal_agent.main.PlanSession", return_value=plan), patch("personal_agent.main.get_model_message", side_effect=model), \
                 patch("personal_agent.main.confirm_in_terminal", side_effect=approve), patch("builtins.input", side_effect=inputs()):
                code, output, errors = invoke(["--session", "commands", "--with-tools", "--accept-edits", "--allow-tool", "write_file"])
            assert code == 0 and "立即提炼失败" in errors and "没有可提交的反馈窗口" in errors, errors
            assert counters == {"plan": 2, "execute": 2, "draft": 2} and len(calls) == 7
            assert len(approvals) == 2 and all(item["completed_messages_preserved"] > 2 for item in checkpoints)
            history = Session("commands").load()
            assert all(not item["content"].startswith(("/plan", "/skill-feedback", "/extract_now")) for item in history if item["role"] == "user")
            assert (root / "playground/result.txt").read_text(encoding="utf-8") == "APPROVED_RESULT"
            feedback = store.list_records("feedback")
            rated = [record for record in feedback if "rating" in record]
            assert {record["rating"] for record in rated} == {"2", "helpful"}
            assert json.loads(store.prepare_draft(rated[0]["id"])[1]["content"])["feedback_record"]["rating"] == rated[0]["rating"]
            assert all(record["task"].startswith("EXEC_TASK") for record in feedback)
            assert all(record["status"] == "pending" for record in store.list_records("candidate")) and skill.read_bytes() == original
            assert all(item["worker_thread"] for item in calls if item["route"] == "draft")
            assert "逻辑调用 7 次" in output and "[评分=2]" in output
            for name in ("plan", "skill-feedback", "extract_now"):
                try:
                    create_project_skill(name, "保留名", "不能创建")
                except ValueError:
                    pass
                else:
                    raise AssertionError(f"command name was not reserved: {name}")
            return {"status": "passed", "validation": "fixed model/usage; actual CLI, terminal n/y, Plan checkpoints, tools, sessions and background threads",
                    "model_callbacks": calls, "checkpoints": checkpoints, "trace": trace(history), "ratings": [record["rating"] for record in rated],
                    "pending_candidates": len(store.list_records("candidate")), "active_skill_unchanged": True,
                    "duplicate_extraction_without_window_blocked": True, "command_names_reserved": True}


def verify_guards():
    checks = {}
    with tempfile.TemporaryDirectory(prefix="personal-repl-guards-") as temporary:
        root = Path(temporary).resolve()
        with isolated(root), patch("personal_agent.main.get_model_settings", return_value=FIXED_SETTINGS):
            skill = create_project_skill("explain-flow", "解释", "解释已知参数和返回值。")
            original = skill.read_bytes()
            store = SkillEvolutionStore()
            session = Session("budget", with_tools=True)
            session.create()
            session.save(HISTORY)
            def budget_inputs():
                yield "/plan"
                yield "/skill-feedback explain-flow negative 请补充依据。"
                yield "/skill-learn explain-flow"
                yield "/extract_now 不应越过零预算。"
                assert len(store.list_records("feedback")) == 1
                yield "/skill-learn off"
                yield "exit"
            with patch("personal_agent.main.get_model_message", side_effect=AssertionError("zero budget must not request model")) as model, patch("builtins.input", side_effect=budget_inputs()):
                code, _, errors = invoke(["--resume", "budget", "--max-cost", "0", "--input-price", "1", "--output-price", "1"])
            assert code == 0 and "预算" in errors and "立即提炼失败" in errors
            assert Session("budget").load() == HISTORY and model.call_count == 0
            checks["zero_budget_keeps_local_plan_and_rating_available_but_blocks_extraction"] = True

            for tool, initial in (("enter_plan_mode", "default"), ("exit_plan_mode", "plan")):
                plan = PlanSession(initial)
                if plan.plan_file:
                    path = root / plan.plan_file
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("此计划不得被审批。", encoding="utf-8")
                state = plan.state
                with patch("personal_agent.main.PlanSession", return_value=plan), \
                     patch("personal_agent.main.confirm_in_terminal", side_effect=AssertionError("deny must not ask")), \
                     patch("personal_agent.main.get_model_message", side_effect=AssertionError("local command must not request")), \
                     patch("builtins.input", side_effect=["/plan", "/plan status", "exit"]):
                    code, output, errors = invoke(["--deny-tool", tool])
                assert code == 0 and not errors and "显式 deny" in output and plan.state == state
            checks["deny_blocks_both_plan_controls_without_approval"] = True

            with patch("personal_agent.main.get_model_message", side_effect=AssertionError("text mode must not start Plan")), \
                 patch("builtins.input", side_effect=["/plan", "/plan status", "/skill-feedback", "/extract_now", "exit"]):
                code, output, errors = invoke(["--session", "text"])
            assert code == 0 and "Plan 切换需要工具聊天" in errors and "立即提炼失败" in errors
            assert Session("text").load() == [] and "用法：/skill-feedback" in output
            checks["text_plan_and_missing_window_do_not_start_tasks"] = True

            # 单次提示继续按普通文本处理，不解释新增 REPL 命令。
            def plain(messages, definitions, *, on_usage, **options):
                assert messages[-1]["content"].startswith("/extract_now")
                on_usage({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                return {"role": "assistant", "content": "PLAIN_PROMPT"}
            with patch("personal_agent.main.get_model_message", side_effect=plain), patch("builtins.input", side_effect=AssertionError("single mode must not read stdin")):
                code, output, _ = invoke(["--prompt", "/extract_now 这是一条普通提示。", "--output-format", "json"])
            assert code == 0 and json.loads(output)["answer"] == "PLAIN_PROMPT"
            checks["single_run_keeps_slash_prompt_plain"] = True

            before = len(store.list_records("feedback"))
            try:
                store.record_feedback("explain-flow", "反馈", HISTORY, rating="x" * 65)
            except ValueError:
                pass
            else:
                raise AssertionError("invalid rating was saved")
            assert len(store.list_records("feedback")) == before and skill.read_bytes() == original
            checks["invalid_rating_has_no_persisted_record"] = True
            return {"status": "passed", "validation": "fixed callbacks; actual CLI, permissions, budget and file/session persistence", "checks": checks}


def verify_live():
    settings = get_model_settings()
    if settings["protocol"] != "openai":
        return {"status": "skipped", "reason": "current protocol is not openai; remote compatibility not inferred"}
    requests, usage = [], UsageStats()
    main_thread = threading.get_ident()
    with tempfile.TemporaryDirectory(prefix="personal-repl-live-") as temporary:
        root = Path(temporary).resolve()
        with isolated(root):
            skill = create_project_skill("explain-flow", "解释函数的参数与返回值", "依据给定函数说明行为，不补写未知实现。")
            original = skill.read_bytes()
            store = SkillEvolutionStore()
            def model(messages, definitions=None, *, on_usage, **options):
                row = {"route": "draft" if messages[0]["content"] == DRAFT_PROMPT else "task",
                       "worker_thread": threading.get_ident() != main_thread, "tools_provided": bool(definitions)}
                requests.append(row)
                def capture(reported):
                    row["usage"] = reported
                    usage.record(reported)
                    on_usage(reported)
                return get_model_message(messages, definitions, on_usage=capture, **options)
            commands = ["只依据这段代码，用一句话说明返回值，不调用工具：def add(a, b): return a + b。",
                        "/skill-feedback explain-flow needs-detail 下次解释时分别说明参数含义和返回值，不补写未知调用方。",
                        "/skill-learn explain-flow", "/extract_now 提炼解释函数时分别说明参数与返回值、缺少调用方材料就标注未知的方法。", "exit"]
            with patch("personal_agent.main.get_model_message", side_effect=model), patch("builtins.input", side_effect=commands):
                code, output, errors = invoke(["--protocol", "openai", "--model", settings["model"], "--max-output-tokens", "1024", "--session", "live-commands"])
            assert code == 0 and not errors, errors
            job = json.loads(next((root / ".personal/skill-jobs").glob("*.json")).read_text(encoding="utf-8"))
            assert job["status"] in {"completed", "discarded"} and skill.read_bytes() == original
            assert len(requests) == 2 and requests[1]["worker_thread"] and not any(row["tools_provided"] for row in requests)
            candidates = store.list_records("candidate")
            assert all(row["status"] == "pending" for row in candidates)
            history = Session("live-commands").load()
            assert len(history) == 2 and store.list_records("feedback")[0].get("rating") == "needs-detail"
            return {"status": "passed", "validation": "real current model API for source answer and background extraction; real CLI/ratings/session",
                    "model": settings["model"], "requests": requests, "total_tokens": usage.total_tokens,
                    "source_answer": history[-1]["content"], "job_status": job["status"],
                    "candidate_statuses": [row["status"] for row in candidates], "active_skill_unchanged": True,
                    "quality_boundary": "one authored task; no quality or generalization improvement claim"}


def entrypoint():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "codex/repl-commands-check-2026-10-29.json")
    args = parser.parse_args()
    report = {"workflow": verify_workflow(), "guards": verify_guards()}
    if args.live:
        report["live"] = verify_live()
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "passed", "output": str(args.output), "live": args.live}, ensure_ascii=False))


if __name__ == "__main__":
    entrypoint()
