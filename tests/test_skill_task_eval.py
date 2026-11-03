"""两条文件任务主流程：独立工程/实际产物，以及权限、add 对照、预算和取消。"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from personal_agent.main import run_repl
from personal_agent.skill_eval import SkillEvaluator
from personal_agent.skills import load_skill
from personal_agent.tools import read_file
from personal_agent.usage import TokenPrices
from personal_agent.tool_registry import TOOL_REGISTRY
from tests.test_skill_eval import setup_candidate
from tests.test_skill_decision import proposal


def calls(*items):
    return {"role": "assistant", "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}} for call_id, name, arguments in items]}


def write_suite(root, *, name="explain-flow", large=False):
    files = {"playground/input.json": '{"values":[3,6]}\n'}
    if large:
        files["playground/notes.txt"] = "NOTE_CONTENT " + "a" * 14000
    suite = {"version": 1, "mode": "file_tools", "name": "file-task-demo", "split": "holdout", "skill_name": name,
             "cases": [{"id": "one", "prompt": "读取 playground/input.json，将 values 之和写入 playground/result.json，只含 count 字段，不修改输入。", "files": files,
                        "expected_files": {"playground/result.json": {"json": {"count": 9}}}, "required_tools": ["read_file", "write_file"]}]}
    path = root / "tool-suite.json"
    path.write_text(json.dumps(suite), encoding="utf-8")
    return path, suite


class SkillTaskEvaluationSmokeTests(unittest.TestCase):
    def test_cli_uses_independent_workspaces_real_parallel_reads_and_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch("personal_agent.skills.PROJECT_ROOT", root), patch("personal_agent.skills.USER_SKILLS_ROOT", root / "user-skills"), patch("personal_agent.tools.PROJECT_ROOT", root), patch("personal_agent.tool_output.PROJECT_ROOT", root):
                path, store, _, candidate = setup_candidate(root)
                original = path.read_bytes()
                original_candidate = store._path("candidate", candidate["id"]).read_bytes()
                suite_path, _ = write_suite(root, large=True)
                (root / "canary.txt").write_text("OUTSIDE_EVALUATION", encoding="utf-8")
                evaluator = SkillEvaluator()
                requests = []

                def model(messages, definitions=None, *, on_usage, **options):
                    self.assertEqual(set(item["name"] for item in definitions), {"read_file", "list_files", "grep_search", "write_file", "edit_file"})
                    self.assertNotIn("expected_files", str(messages))
                    self.assertNotIn("SOURCE_TASK_ONLY", str(messages))
                    self.assertNotIn("OUTSIDE_EVALUATION", str(messages))
                    self.assertEqual(options["max_tokens"], 1024)
                    on_usage({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                    requests.append(messages)
                    if len(messages) == 2:
                        return calls(("read", "read_file", {"file_path": "playground/input.json"}), ("notes", "read_file", {"file_path": "playground/notes.txt"}))
                    if messages[-1]["tool_call_id"] == "notes":
                        self.assertIn("工具结果已截断", messages[-1]["content"])
                        self.assertIn("3,6", messages[-2]["content"])
                        if "EXPANDED_METHOD" in messages[0]["content"]:
                            return calls(("write", "write_file", {"file_path": "playground/result.json", "content": '{"count":9}'}))
                    return {"role": "assistant", "content": "声称已完成，实际文件由本地检查核对。"}

                def inputs():
                    yield f"/skill-eval-tools {candidate['id']} {suite_path}"
                    report = evaluator.list_reports()[0]
                    yield f"/skill-evals {report['id']}"
                    yield "/usage"
                    yield "exit"

                output, errors = io.StringIO(), io.StringIO()
                with patch("personal_agent.main.get_model_message", side_effect=model), patch("builtins.input", side_effect=inputs()), redirect_stdout(output), redirect_stderr(errors):
                    history = run_repl(permission_mode="acceptEdits")
                report = evaluator.list_reports()[0]
                self.assertEqual(report["decision"], "candidate_better_on_tasks")
                self.assertFalse(report["rows"][0]["all_passed"])
                self.assertTrue(report["rows"][1]["all_passed"])
                self.assertEqual(report["usage_summary"]["reported_tokens"]["total_tokens"], 10)
                self.assertEqual(len(requests), 5)
                self.assertEqual(history, [])
                self.assertEqual(errors.getvalue(), "")
                self.assertIn("逻辑调用 5 次", output.getvalue())
                self.assertIn("实际工具轨迹", output.getvalue())
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(store._path("candidate", candidate["id"]).read_bytes(), original_candidate)
                self.assertFalse((root / ".personal/tool-results").exists())
                for row in report["rows"]:
                    workspace = root / row["workspace"]
                    self.assertEqual((workspace / "playground/input.json").read_text(), '{"values":[3,6]}\n')
                    self.assertEqual(len(list((workspace / ".personal/tool-results").glob("*.txt"))), 1)
                self.assertIn("OUTSIDE_EVALUATION", read_file("canary.txt"))  # ContextVar 已恢复。

    def test_add_control_path_permissions_budget_and_cancellation_keep_partial_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch("personal_agent.skills.PROJECT_ROOT", root), patch("personal_agent.skills.USER_SKILLS_ROOT", root / "user-skills"), patch("personal_agent.tools.PROJECT_ROOT", root):
                _, store, feedback, _ = setup_candidate(root)
                request = store.prepare_draft(feedback["id"])
                added = store.save_draft(feedback["id"], proposal("add", "new-file-method", "NEW_METHOD 先读后写，检查结果。", "文件方法"), request=request)
                suite_path, suite = write_suite(root, name=added["skill_name"])
                evaluator = SkillEvaluator()
                canary = root / "canary.txt"
                canary.write_text("UNCHANGED", encoding="utf-8")

                def unsafe(messages, definitions, observe):
                    observe(None)
                    if len(messages) == 2:
                        return calls(("escape", "write_file", {"file_path": str(canary), "content": "BAD"}), ("shell", "run_shell", {"command": "must-not-run"}), ("read", "read_file", {"file_path": "playground/input.json"}))
                    return {"role": "assistant", "content": "声称完成"}

                shell = Mock(side_effect=AssertionError("Shell must not execute"))
                with patch.dict(TOOL_REGISTRY["run_shell"], {"handler": shell}):
                    report = evaluator.evaluate_tools(added["id"], suite_path, unsafe, model_settings={}, permission_mode="bypassPermissions")
                shell.assert_not_called()
                self.assertEqual(report["baseline_kind"], "no_skill_control")
                self.assertEqual(canary.read_text(), "UNCHANGED")
                self.assertTrue(all(not row["all_passed"] for row in report["rows"]))
                self.assertTrue(all([item["succeeded"] for item in row["trace"]] == [False, False, True] for row in report["rows"]))
                self.assertFalse((root / added["skill_path"]).exists())

                def normal(messages, definitions, observe):
                    observe({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                    if len(messages) == 2:
                        return calls(("read", "read_file", {"file_path": "playground/input.json"}))
                    if messages[-1]["tool_call_id"] == "read":
                        return calls(("write", "write_file", {"file_path": "playground/result.json", "content": '{"count":9}'}))
                    return {"role": "assistant", "content": "完成"}

                success = evaluator.evaluate_tools(added["id"], suite_path, normal, model_settings={}, permission_mode="acceptEdits")
                self.assertEqual(success["baseline_kind"], "no_skill_control")
                self.assertEqual(success["methods"]["baseline"]["text"], "")
                self.assertTrue(all(row["all_passed"] for row in success["rows"]))
                self.assertFalse((root / added["skill_path"]).exists())
                plan = evaluator.evaluate_tools(added["id"], suite_path, normal, model_settings={}, permission_mode="plan", allow=["write_file"])
                self.assertTrue(all(not row["all_passed"] for row in plan["rows"]))
                self.assertTrue(all("权限拒绝" in row["trace"][1]["result"] for row in plan["rows"]))

                def cancelled(messages, definitions, observe):
                    if messages[-1].get("tool_call_id") == "write":
                        observe(None)
                        raise KeyboardInterrupt
                    return normal(messages, definitions, observe)

                partial = evaluator.evaluate_tools(added["id"], suite_path, cancelled, model_settings={}, permission_mode="acceptEdits")
                self.assertEqual(partial["status"], "cancelled")
                self.assertIsNone(partial["rows"][0]["score"])
                self.assertEqual(partial["rows"][1]["status"], "not_run")
                self.assertEqual(json.loads((root / partial["rows"][0]["workspace"] / "playground/result.json").read_text()), {"count": 9})

                suite["cases"][0]["files"] = {"../escape.txt": "bad"}
                suite_path.write_text(json.dumps(suite), encoding="utf-8")
                callback = Mock()
                with self.assertRaisesRegex(ValueError, "playground"):
                    evaluator.evaluate_tools(added["id"], suite_path, callback, model_settings={})
                callback.assert_not_called()
                suite_path, _ = write_suite(root, name=added["skill_name"])
                def budget_model(messages, definitions=None, *, on_usage, **options):
                    on_usage({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                    return calls(("write", "write_file", {"file_path": "playground/result.json", "content": '{"count":9}'}))
                before = {item["id"] for item in evaluator.list_reports()}
                with patch("personal_agent.main.get_model_message", side_effect=budget_model) as model, patch("builtins.input", side_effect=[f"/skill-eval-tools {added['id']} {suite_path}", "exit"]), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    run_repl(prices=TokenPrices(Decimal(1), Decimal(1)), max_cost=Decimal("0.000002"))
                model.assert_called_once()
                budget = next(item for item in evaluator.list_reports() if item["id"] not in before)
                self.assertEqual(budget["status"], "partial")
                self.assertEqual(budget["usage_summary"]["logical_calls_started"], 1)
                self.assertFalse((root / budget["rows"][0]["workspace"] / "playground/result.json").exists())
                self.assertFalse((root / added["skill_path"]).exists())
                self.assertEqual(load_skill(root / feedback["skill_path"]).name, "explain-flow")


if __name__ == "__main__":
    unittest.main()
