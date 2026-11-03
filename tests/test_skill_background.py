"""两条后台主流程：窗口隔离/真实线程，以及取消、版本和共享预算。"""

import io
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from personal_agent.main import run_repl
from personal_agent.session import Session
from personal_agent.skill_background import SkillDraftJobs
from personal_agent.skill_evolution import DRAFT_PROMPT, SkillEvolutionStore
from personal_agent.skills import create_project_skill
from personal_agent.usage import TokenPrices, UsageStats
from tests.test_skill_evolution import draft_reply


SETTINGS = {"model": "scripted"}
HISTORY = [{"role": "user", "content": "TARGET_TASK"}, {"role": "assistant", "content": "BASE_ANSWER"}]


class SkillBackgroundSmokeTests(unittest.TestCase):
    def test_cli_window_freezes_source_and_background_does_not_pollute_history_or_adopt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch("personal_agent.skills.PROJECT_ROOT", root), patch("personal_agent.skills.USER_SKILLS_ROOT", root / "user-skills"), patch("personal_agent.session.PROJECT_ROOT", root):
                path = create_project_skill("explain-flow", "解释", "BASE_METHOD $ARGUMENTS")
                original = path.read_bytes()
                store = SkillEvolutionStore()
                started, release = threading.Event(), threading.Event()
                threads = []
                main_thread = threading.get_ident()

                def model(messages, definitions=None, *, on_usage, **options):
                    if messages[0]["content"] == DRAFT_PROMPT:
                        threads.append(threading.get_ident())
                        self.assertIsNone(definitions)
                        self.assertEqual(options["max_tokens"], 1024)
                        self.assertEqual(len(messages), 2)
                        self.assertNotIn("PRIVATE_OLD_HISTORY", str(messages))
                        self.assertIn("TARGET_TASK", str(messages))
                        self.assertIn("缺少返回值", str(messages))
                        started.set()
                        self.assertTrue(release.wait(3))
                        on_usage({"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4})
                        return draft_reply()
                    self.assertEqual(threading.get_ident(), main_thread)
                    on_usage({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                    return {"role": "assistant", "content": "BASE_ANSWER"}

                def inputs():
                    yield "PRIVATE_OLD_HISTORY"
                    yield "TARGET_TASK"
                    yield "/skill-learn explain-flow"
                    yield "/skills"
                    yield "缺少返回值，请给出清晰说明。"
                    self.assertTrue(started.wait(3))
                    self.assertEqual(store.list_records("candidate"), [])
                    yield "/skill-jobs"
                    yield "/usage"  # 后台受屏障阻塞，本地查询依然可运行。
                    release.set()
                    yield "/skill-jobs wait"
                    candidate = store.list_records("candidate")[0]
                    self.assertEqual(candidate["status"], "pending")
                    self.assertEqual(store.feedback(candidate["feedback_id"])["task"], "TARGET_TASK")
                    self.assertEqual(path.read_bytes(), original)
                    yield f"/skill-candidates {candidate['id']}"
                    yield "普通追问"
                    yield "/skill-learn explain-flow"
                    yield "/skill-learn off"
                    yield "/skill-learn explain-flow"
                    yield "/clear"
                    yield "清空后普通问题"
                    yield "/usage"
                    yield "exit"

                output, errors = io.StringIO(), io.StringIO()
                try:
                    with patch("personal_agent.main.get_model_message", side_effect=model), patch("builtins.input", side_effect=inputs()), redirect_stdout(output), redirect_stderr(errors):
                        history = run_repl(session_name="background", with_tools=True)
                finally:
                    release.set()
                self.assertEqual(len(threads), 1)
                self.assertNotEqual(threads[0], main_thread)
                self.assertEqual(history, [{"role": "user", "content": "清空后普通问题"}, {"role": "assistant", "content": "BASE_ANSWER"}])
                self.assertEqual(Session("background").load(), history)
                self.assertEqual(len(store.list_records("feedback")), 1)
                self.assertIn("逻辑调用 5 次", output.getvalue())
                self.assertIn("[completed]", output.getvalue())
                self.assertEqual(errors.getvalue(), "")
                self.assertEqual(path.read_bytes(), original)

    def test_cancel_stale_version_and_background_cost_block_commit_or_foreground_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch("personal_agent.skills.PROJECT_ROOT", root), patch("personal_agent.skills.USER_SKILLS_ROOT", root / "user-skills"):
                path = create_project_skill("explain-flow", "解释", "BASE_METHOD")
                original = path.read_bytes()
                store, usage = SkillEvolutionStore(), UsageStats()
                for action in ("cancel", "edit", "exit"):
                    started, release = threading.Event(), threading.Event()

                    def raw(request, observer):
                        started.set()
                        self.assertTrue(release.wait(3))
                        observer({"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4})
                        return draft_reply()

                    jobs = SkillDraftJobs(raw, usage.record, lambda: None, model_settings=SETTINGS)
                    try:
                        jobs.arm("explain-flow", HISTORY)
                        job = jobs.submit_feedback("补充解释")
                        self.assertTrue(started.wait(3))
                        if action == "cancel":
                            jobs.cancel(job["id"])
                        elif action == "edit":
                            path.write_bytes(original + b"\nexternal change")
                        release.set()
                        if action == "exit":
                            with redirect_stdout(io.StringIO()):
                                jobs.close()
                            result = jobs.load(job["id"])
                        else:
                            result = jobs.poll(wait=True)
                        self.assertEqual(result["status"], "failed" if action == "edit" else "cancelled")
                        self.assertEqual(store.list_records("candidate"), [])
                        self.assertTrue(result["logical_call_started"])
                    finally:
                        release.set()
                        jobs.close()
                        path.write_bytes(original)
                self.assertEqual(usage.calls, 3)
                self.assertEqual(usage.total_tokens, 12)

                prices = TokenPrices(Decimal(1), Decimal(1))
                empty = UsageStats()
                jobs = SkillDraftJobs(lambda *_: self.fail("zero budget must not call model"), empty.record, lambda: empty.check_budget(prices, Decimal(0)), model_settings=SETTINGS)
                try:
                    jobs.arm("explain-flow", HISTORY)
                    count = len(store.list_records("feedback"))
                    with self.assertRaisesRegex(RuntimeError, "预算"):
                        jobs.submit_feedback("不能发送")
                    self.assertEqual(len(store.list_records("feedback")), count)
                    self.assertEqual(empty.calls, 0)
                finally:
                    jobs.close()

                started, release = threading.Event(), threading.Event()
                foreground = []
                def model(messages, definitions=None, *, on_usage, **options):
                    if messages[0]["content"] == DRAFT_PROMPT:
                        started.set()
                        self.assertTrue(release.wait(3))
                        on_usage({"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4})
                        return draft_reply()
                    foreground.append(messages)
                    on_usage({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                    return {"role": "assistant", "content": "BASE_ANSWER"}

                def inputs():
                    yield "TARGET_TASK"
                    yield "/skill-learn explain-flow"
                    yield "补充返回值"
                    self.assertTrue(started.wait(3))
                    release.set()
                    yield "NEXT_TASK_MUST_NOT_SEND"
                    yield "/usage"
                    yield "exit"

                output, errors = io.StringIO(), io.StringIO()
                try:
                    with patch("personal_agent.main.get_model_message", side_effect=model), patch("builtins.input", side_effect=inputs()), redirect_stdout(output), redirect_stderr(errors):
                        history = run_repl(prices=prices, max_cost=Decimal("0.000006"))
                finally:
                    release.set()
                self.assertEqual(len(foreground), 1)
                self.assertEqual(history, HISTORY)
                self.assertIn("预算", errors.getvalue())
                self.assertIn("逻辑调用 2 次", output.getvalue())
                self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
