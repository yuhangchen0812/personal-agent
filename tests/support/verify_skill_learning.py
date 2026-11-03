"""一次验证连续窗口、真实使用来源及恢复边界；模型固定，CLI/文件/线程实际运行。"""

import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from personal_agent.main import run_repl
from personal_agent.session import Session
from personal_agent.skill_background import SkillDraftJobs
from personal_agent.skill_evolution import DRAFT_PROMPT, SkillEvolutionStore
from personal_agent.skill_learning import SkillLearning, list_skill_tasks, skill_learning_scope
from personal_agent.skill_lifecycle import SkillLifecycleStore, record_skill_event
from personal_agent.skills import create_project_skill, load_skill


def reject(call):
    try:
        call()
    except ValueError:
        return
    raise AssertionError("invalid evidence or operation accepted")


def isolated(root):
    from contextlib import ExitStack
    stack = ExitStack()
    for target, value in (("personal_agent.skills.PROJECT_ROOT", root), ("personal_agent.skills.USER_SKILLS_ROOT", root / "user-skills"),
            ("personal_agent.session.PROJECT_ROOT", root)):
        stack.enter_context(patch(target, value))
    return stack


def inspect_restore(root):
    with isolated(root):
        before = {str(p): p.read_bytes() for p in (root / ".personal/skill-jobs").glob("*.json")}
        output = io.StringIO()
        with patch("personal_agent.main.get_model_message", side_effect=AssertionError("restored tools/models must not execute")), \
                patch("builtins.input", side_effect=["/skill-learn", "/skill-uses explain-flow", "exit"]), redirect_stdout(output):
            history = run_repl(session_name="learning", resume=True)
        assert history == Session("learning").load()
        assert '"enabled": false' in output.getvalue() and '"feedback_ids"' in output.getvalue()
        assert {str(p): p.read_bytes() for p in (root / ".personal/skill-jobs").glob("*.json")} == before
    return {"status": "passed", "fresh_process_mode_off_no_tool_model_or_job_replay": True}


def verify_workflow():
    with tempfile.TemporaryDirectory(prefix="skill-learning-中文 空格-") as temporary:
        root = Path(temporary).resolve()
        with isolated(root):
            path = create_project_skill("explain-flow", "解释调用链", "BASE_METHOD $ARGUMENTS")
            original = path.read_bytes()
            requests, drafts = [], []
            store, files = SkillEvolutionStore(), SkillLifecycleStore()

            def model(messages, definitions=None, *, on_usage, **options):
                on_usage({"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3})
                if messages[0]["content"] == DRAFT_PROMPT:
                    payload = json.loads(messages[1]["content"])
                    feedback = payload["feedback_record"]
                    drafts.append(feedback)
                    assert "task_use" in feedback and "BEFORE_AUTO" not in str(payload)
                    merge = len(drafts) == 1
                    value = {"action": "merge" if merge else "discard", "target_skill": "explain-flow" if merge else "",
                        "description": "", "lesson": "先说明输入与返回，再解释关键调用。" if merge else "", "rationale": "用户明确纠正顺序。" if merge else "没有新的持久纠正。"}
                    return {"role": "assistant", "content": json.dumps(value, ensure_ascii=False)}
                requests.append(messages[-1])
                task = next(item["content"] for item in reversed(messages) if item["role"] == "user")
                if "FAIL_TASK" in task:
                    raise RuntimeError("expected foreground failure")
                if "CANCEL_TASK" in task:
                    raise KeyboardInterrupt()
                if "STALE_TASK" in task:
                    path.write_bytes(original + b"\nchanged after load\n")
                if messages[-1]["role"] == "user" and "FEEDBACK_NEXT 先说明输入和返回，再解释调用链" in task:
                    return {"role": "assistant", "content": "", "tool_calls": [{"id": "load-method", "type": "function",
                        "function": {"name": "skill", "arguments": json.dumps({"skill_name": "explain-flow", "args": "FOLLOWUP"})}}]}
                return {"role": "assistant", "content": "FIXED_ANSWER"}

            inputs = ["/explain-flow BEFORE_AUTO", "/skill-learn auto", "/explain-flow SOURCE_TASK", "/skill-uses",
                "FEEDBACK_NEXT 先说明输入和返回，再解释调用链", "/extract_now", "/skill-uses explain-flow", "/skill-learn off",
                "/explain-flow FAIL_TASK", "/explain-flow CANCEL_TASK", "/skill-learn auto explain-flow", "/explain-flow STALE_TASK",
                "AFTER_STALE 正常聊天", "/skill-learn off", "exit"]
            output, errors = io.StringIO(), io.StringIO()
            with patch("personal_agent.main.get_model_message", side_effect=model), patch("builtins.input", side_effect=inputs), \
                    redirect_stdout(output), redirect_stderr(errors):
                history = run_repl(session_name="learning", with_tools=True)
            path.write_bytes(original)
            assert len(drafts) == 2 and "SOURCE_TASK" in drafts[0]["task"], {"drafts": drafts, "output": output.getvalue(), "errors": errors.getvalue()}
            assert drafts[1]["task"] == "FEEDBACK_NEXT 先说明输入和返回，再解释调用链"
            assert any(item["role"] == "user" and drafts[1]["task"] in item["content"] for item in requests)
            assert any(item["role"] == "tool" for item in requests), output.getvalue()
            candidates = store.list_records("candidate")
            assert len(candidates) == 1 and candidates[0]["status"] == "pending" and path.read_bytes() == original
            rows = list_skill_tasks(files)
            assert len(rows) == 6 and {row["status"] for row in rows} == {"loop_completed", "failed", "cancelled"}, rows
            assert len(store.list_records("feedback")) == 2
            assert "Skill 版本已经变化" in errors.getvalue() and "expected foreground failure" in errors.getvalue()
            assert history[-2]["content"] == "AFTER_STALE 正常聊天"

            # 来源变化必须阻止反馈再次读取/生成草稿，恢复原字节后仍可读取。
            ref = drafts[0]["task_use"]
            journal = files._path(f".personal/skill-usage/tasks/{ref['id']}.json")
            task = json.loads(journal.read_bytes())
            event = files._path(f".personal/skill-usage/events/{task['uses'][0]['event_id']}.json")
            for target in (journal, event):
                raw = target.read_bytes()
                target.write_bytes(raw + b"\n")
                reject(lambda: store.prepare_draft(drafts[0]["id"]))
                target.write_bytes(raw)
            store.prepare_draft(drafts[0]["id"])

            # 预算/Plan 拒绝消费窗口但不创建反馈，多个实际方法也不猜目标。
            def no_budget():
                raise ValueError("zero budget")
            def unexpected(*args):
                raise AssertionError("no background request expected")
            jobs = SkillDraftJobs(unexpected, unexpected, no_budget, model_settings={"model": "fixed"})
            try:
                learning = SkillLearning(jobs)
                with skill_learning_scope(learning):
                    learning.enable()
                    for allowed in (True, False):
                        learning.begin("BOUNDED_TASK")
                        record_skill_event(load_skill(path), "inline_loaded", "verification")
                        learning.finish("loop_completed", [{"role": "user", "content": "BOUNDED_TASK"}, {"role": "assistant", "content": "ANSWER"}], answer="ANSWER")
                        reject(lambda: learning.submit_next("用户纠正", allowed=allowed))
                        assert learning.pending is None and jobs.window is None and jobs.future is None
                    other = create_project_skill("other-method", "其他方法", "OTHER_METHOD")
                    learning.begin("TWO_METHODS")
                    for selected in (path, other):
                        record_skill_event(load_skill(selected), "inline_loaded", "verification")
                    learning.finish("loop_completed", [{"role": "user", "content": "TWO_METHODS"}, {"role": "assistant", "content": "ANSWER"}], answer="ANSWER")
                    assert learning.pending is None
                    learning.reset()
                    assert learning.last is None
            finally:
                jobs.close()
            assert len(store.list_records("feedback")) == 2
            output = io.StringIO()
            with patch("builtins.input", side_effect=["/skill-learn auto", "exit"]), redirect_stdout(output):
                with redirect_stderr(output):
                    run_repl(permission_mode="plan")
            assert "Plan 期间不能开启" in output.getvalue()
        process = subprocess.run([sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()), "--inspect-root", str(root)],
            cwd=root, capture_output=True, timeout=60)
        assert process.returncode == 0, process.stderr.decode("utf-8", errors="replace")
        return {"status": "passed", "validation": "fixed model; actual CLI, file tools, background thread, event journals and fresh process",
            "automatic_window_next_input_is_also_normal_chat": True, "manual_and_model_loaded_skills_recorded_with_exact_byte_versions": True,
            "feedback_task_event_binding_and_tamper_rejection": True, "extract_now_pending_and_no_auto_adoption": True,
            "failed_cancelled_stale_ambiguous_plan_budget_paths": True, "fresh_process_mode_off_no_replay": True,
            "new_remote_model_calls": 0}


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--inspect-root":
        print(json.dumps(inspect_restore(Path(sys.argv[2])), ensure_ascii=False))
    else:
        print(json.dumps(verify_workflow(), ensure_ascii=False))
