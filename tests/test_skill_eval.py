"""三条回放主流程：成对比较、规则和版本边界、预算/取消部分报告。"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from personal_agent.main import run_repl
from personal_agent.session import Session
from personal_agent.skill_eval import REPLAY_PROMPT, SkillEvaluator
from personal_agent.skill_evolution import SkillEvolutionStore
from personal_agent.skills import create_project_skill
from personal_agent.usage import TokenPrices


def setup_candidate(root):
    path = create_project_skill("explain-flow", "解释", "BASE_METHOD $ARGUMENTS")
    store = SkillEvolutionStore()
    feedback = store.record_feedback("explain-flow", "解释缺少结构", [{"role": "user", "content": "SOURCE_TASK_ONLY"}, {"role": "assistant", "content": "原回答"}])
    candidate = store.draft(feedback["id"], lambda _: {"role": "assistant", "content": json.dumps({"lesson": "EXPANDED_METHOD 使用结构化解释。", "rationale": "回应反馈"})})
    return path, store, feedback, candidate


def write_suite(root, cases=None):
    suite = {"version": 1, "name": "independent-toy", "split": "holdout", "skill_name": "explain-flow", "cases": cases or [
        {"id": "first", "prompt": "CASE_ONE：解释一个新函数，材料仅此。", "rules": {"contains": ["参数", "返回值", "调用顺序"], "not_contains": ["RULE_ONLY_MARKER"], "max_chars": 500}},
        {"id": "second", "prompt": "CASE_TWO：说明另一个新函数，材料仅此。", "rules": {"json_keys": ["inputs", "outputs"], "not_contains": ["RULE_ONLY_MARKER"]}},
    ]}
    path = root / "suite.json"
    path.write_text(json.dumps(suite, ensure_ascii=False), encoding="utf-8")
    return path, suite


class SkillEvaluationSmokeTests(unittest.TestCase):
    def test_repl_pairs_isolated_requests_and_saves_rule_evidence_without_adopting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch("personal_agent.skills.PROJECT_ROOT", root), \
                 patch("personal_agent.skills.USER_SKILLS_ROOT", root / "user-skills"), \
                 patch("personal_agent.session.PROJECT_ROOT", root), \
                 patch("personal_agent.main.discover_agents", return_value=([], [])):
                path, store, _, candidate = setup_candidate(root)
                original = path.read_bytes()
                candidate_bytes = store._path("candidate", candidate["id"]).read_bytes()
                suite_path, _ = write_suite(root)
                requests = []

                def model(messages, definitions=None, *, on_usage, **options):
                    on_usage({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                    if not messages[0]["content"].startswith(REPLAY_PROMPT):
                        return {"role": "assistant", "content": "普通历史回复"}
                    self.assertIsNone(definitions)
                    self.assertEqual(options["max_tokens"], 1024)
                    self.assertNotIn("PRIVATE_PARENT_CONTEXT", str(messages))
                    self.assertNotIn("RULE_ONLY_MARKER", str(messages))
                    self.assertNotIn("SOURCE_TASK_ONLY", str(messages))
                    self.assertEqual(len(messages), 2)
                    requests.append(messages)
                    enhanced = "EXPANDED_METHOD" in messages[0]["content"]
                    output = (json.dumps({"inputs": "材料中的参数", "outputs": "材料中的返回值"}) if "CASE_TWO" in messages[1]["content"] else "参数、返回值、调用顺序") if enhanced else "简略回答"
                    return {"role": "assistant", "content": output}

                output, errors = io.StringIO(), io.StringIO()
                evaluator = SkillEvaluator()

                def inputs():
                    yield "PRIVATE_PARENT_CONTEXT"
                    yield f"/skill-eval {candidate['id']} {suite_path}"
                    report = evaluator.list_reports()[0]
                    yield "/skill-evals"
                    yield f"/skill-evals {report['id']}"
                    yield "/usage"
                    yield "exit"

                with patch("personal_agent.main.get_model_message", side_effect=model), \
                     patch("builtins.input", side_effect=inputs()), redirect_stdout(output), redirect_stderr(errors):
                    history = run_repl(session_name="comparison", with_tools=True)
                report = evaluator.list_reports()[0]
                self.assertEqual(report["status"], "complete")
                self.assertEqual(report["decision"], "candidate_better_on_suite")
                self.assertEqual(report["summary"]["candidate"]["mean_score"], 1)
                self.assertLess(report["summary"]["baseline"]["mean_score"], 1)
                self.assertEqual(len(requests), 4)
                self.assertEqual(report["usage_summary"]["reported_tokens"]["total_tokens"], 8)
                self.assertIsNone(report["usage_summary"]["estimated_cost_yuan"])
                self.assertTrue(report["active_skill_unchanged"])
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(store._path("candidate", candidate["id"]).read_bytes(), candidate_bytes)
                self.assertEqual(len(history), 2)
                self.assertEqual(Session("comparison").load(), history)
                self.assertIn("逻辑调用 5 次", output.getvalue())
                self.assertIn("实际输出", output.getvalue())
                self.assertEqual(errors.getvalue(), "")

    def test_validation_rejects_leaks_and_stale_versions_and_forbidden_rules_override_average(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch("personal_agent.skills.PROJECT_ROOT", root), patch("personal_agent.skills.USER_SKILLS_ROOT", root / "user-skills"):
                path, store, _, candidate = setup_candidate(root)
                original = path.read_bytes()
                evaluator = SkillEvaluator()
                suite_path, suite = write_suite(root, [{"id": "one", "prompt": "SOURCE_TASK_ONLY", "rules": {"contains": ["OK"]}}])
                model = Mock()
                with self.assertRaisesRegex(ValueError, "源任务"):
                    evaluator.evaluate(candidate["id"], suite_path, model, model_settings={})
                suite["cases"][0]["prompt"] = "一个不同的任务"
                suite["cases"][0]["rules"] = {"unsupported": ["x"]}
                suite_path.write_text(json.dumps(suite), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "规则"):
                    evaluator.evaluate(candidate["id"], suite_path, model, model_settings={})
                suite["cases"][0]["rules"] = {"contains": ["OK", "EXTRA", "MORE"], "not_contains": ["forbidden"]}
                suite_path.write_text(json.dumps(suite), encoding="utf-8")
                path.write_bytes(original + b"\nexternal edit")
                with self.assertRaisesRegex(ValueError, "版本不一致"):
                    evaluator.evaluate(candidate["id"], suite_path, model, model_settings={})
                model.assert_not_called()
                path.write_bytes(original)
                receipt = store.adopt(candidate["id"])
                adopted = path.read_bytes()

                def responses(messages, observe):
                    observe({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                    expanded = "EXPANDED_METHOD" in messages[0]["content"]
                    return {"role": "assistant", "content": "OK EXTRA MORE forbidden" if expanded else "OK"}

                report = evaluator.evaluate(candidate["id"], suite_path, responses, model_settings={"model": "scripted"})
                self.assertGreater(report["summary"]["candidate"]["mean_score"], report["summary"]["baseline"]["mean_score"])
                self.assertEqual(report["decision"], "candidate_blocked_by_rules")
                self.assertNotIn("EXPANDED_METHOD", report["methods"]["baseline"]["text"])
                self.assertEqual(path.read_bytes(), adopted)
                (root / receipt["snapshot"]).write_bytes(b"tampered")
                with self.assertRaisesRegex(ValueError, "版本不一致"):
                    evaluator.evaluate(candidate["id"], suite_path, model, model_settings={})
                model.assert_not_called()

    def test_budget_and_cancellation_save_incomplete_reports_without_inventing_scores(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch("personal_agent.skills.PROJECT_ROOT", root), patch("personal_agent.skills.USER_SKILLS_ROOT", root / "user-skills"):
                path, store, _, candidate = setup_candidate(root)
                before = path.read_bytes()
                suite_path, _ = write_suite(root)
                evaluator = SkillEvaluator()

                def model(*_, on_usage, **__):
                    on_usage({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                    return {"role": "assistant", "content": "简略回答"}

                output = io.StringIO()
                with patch("personal_agent.main.get_model_message", side_effect=model) as request, \
                     patch("builtins.input", side_effect=[f"/skill-eval {candidate['id']} {suite_path}", "exit"]), \
                     redirect_stdout(output), redirect_stderr(io.StringIO()):
                    history = run_repl(prices=TokenPrices(Decimal(1), Decimal(1)), max_cost=Decimal("0.000002"))
                request.assert_called_once()
                report = evaluator.list_reports()[0]
                self.assertEqual(report["status"], "partial")
                self.assertEqual(report["decision"], "incomplete")
                self.assertEqual([row["status"] for row in report["rows"]], ["complete", "error", "not_run", "not_run"])
                self.assertIsNone(report["summary"]["candidate"]["mean_score"])
                self.assertFalse(report["rows"][1]["logical_call_started"])
                self.assertEqual(report["usage_summary"]["logical_calls_started"], 1)
                self.assertIn("预算", output.getvalue())
                self.assertEqual(history, [])
                cancelled = evaluator.evaluate(candidate["id"], suite_path, Mock(side_effect=KeyboardInterrupt), model_settings={})
                self.assertEqual(cancelled["status"], "cancelled")
                self.assertEqual(cancelled["decision"], "incomplete")
                self.assertTrue(all(row["score"] is None for row in cancelled["rows"]))
                self.assertEqual(evaluator.load(cancelled["id"])["status"], "cancelled")
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(store.candidate(candidate["id"])["status"], "pending")


if __name__ == "__main__":
    unittest.main()
