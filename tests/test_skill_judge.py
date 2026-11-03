"""两条联合评估主流程：实际 CLI 留档，以及门槛/裁判失败不晋升。"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from personal_agent.main import run_repl
from personal_agent.skill_eval import JUDGE_PROMPT, SkillEvaluator
from personal_agent.usage import TokenPrices
from tests.test_skill_eval import setup_candidate, write_suite


class SkillJudgeSmokeTests(unittest.TestCase):
    def test_cli_isolated_judge_accounts_for_usage_and_archives_without_deployment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch("personal_agent.skills.PROJECT_ROOT", root), patch("personal_agent.skills.USER_SKILLS_ROOT", root / "user-skills"):
                path, store, _, candidate = setup_candidate(root)
                before = path.read_bytes()
                candidate_before = store._path("candidate", candidate["id"]).read_bytes()
                suite_path, _ = write_suite(root, [{"id": "new", "prompt": "独立任务：解释给出的函数。", "rules": {"contains": ["参数"], "not_contains": ["RULE_PRIVATE"]}}])
                requests = []

                def model(messages, definitions=None, *, on_usage, **options):
                    self.assertIsNone(definitions)
                    self.assertEqual(options["max_tokens"], 1024)
                    self.assertEqual(len(messages), 2)
                    requests.append(messages)
                    on_usage({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                    if messages[0]["content"] == JUDGE_PROMPT:
                        payload = json.loads(messages[1]["content"])
                        self.assertEqual(set(payload), {"task", "answer"})
                        for private in ("EXPANDED_METHOD", "BASE_METHOD", "SOURCE_TASK_ONLY", "RULE_PRIVATE", "candidate", "baseline"):
                            self.assertNotIn(private, str(messages))
                        output = json.dumps({"score": 4 if "完整" in payload["answer"] else 3, "reason": "固定裁判理由"})
                    else:
                        output = "参数：完整解释" if "EXPANDED_METHOD" in messages[0]["content"] else "参数：简略解释"
                    return {"role": "assistant", "content": output}

                evaluator = SkillEvaluator()

                def inputs():
                    yield f"/skill-eval-judge {candidate['id']} {suite_path}"
                    report = evaluator.list_reports()[0]
                    yield "/skill-champions"
                    yield f"/skill-champions {report['id']}"
                    yield f"/skill-evals {report['id']}"
                    yield "/usage"
                    yield "exit"

                output, errors = io.StringIO(), io.StringIO()
                with patch("personal_agent.main.get_model_message", side_effect=model), patch("builtins.input", side_effect=inputs()), redirect_stdout(output), redirect_stderr(errors):
                    history = run_repl(permission_mode="plan", prices=TokenPrices(Decimal(1), Decimal(1)))
                report = evaluator.list_reports()[0]
                archive = evaluator.champion(report["id"])
                self.assertEqual(report["decision"], "candidate_selected")
                self.assertEqual(report["summary"]["candidate"]["mean_judge_score"], 4)
                self.assertEqual(len(requests), 4)
                self.assertEqual(report["usage_summary"]["reported_tokens"]["total_tokens"], 8)
                self.assertEqual(Decimal(report["usage_summary"]["estimated_cost_yuan"]), Decimal("0.000008"))
                self.assertEqual(archive["method"], report["methods"]["candidate"])
                self.assertFalse(archive["deployed"])
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(store._path("candidate", candidate["id"]).read_bytes(), candidate_before)
                self.assertEqual(history, [])
                self.assertEqual(errors.getvalue(), "")
                self.assertIn("逻辑调用 4 次", output.getvalue())
                self.assertIn("裁判原文", output.getvalue())

    def test_rules_semantic_regression_bad_judge_and_budget_prevent_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch("personal_agent.skills.PROJECT_ROOT", root), patch("personal_agent.skills.USER_SKILLS_ROOT", root / "user-skills"):
                path, _, _, candidate = setup_candidate(root)
                before = path.read_bytes()
                suite_path, suite = write_suite(root, [{"id": "new", "prompt": "独立任务", "rules": {"contains": ["OK"], "not_contains": ["forbidden"]}}])
                evaluator = SkillEvaluator()
                answer, judge_score = "OK forbidden", 4

                def replay(messages, observe):
                    observe({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                    return {"role": "assistant", "content": answer if "EXPANDED_METHOD" in messages[0]["content"] else "OK baseline"}

                def judge(messages, observe):
                    observe(None)
                    score = 4 if "baseline" in messages[1]["content"] else judge_score
                    return {"role": "assistant", "content": json.dumps({"score": score, "reason": "固定"})}

                report = evaluator.evaluate(candidate["id"], suite_path, replay, model_settings={}, judge_model=judge)
                self.assertEqual(report["decision"], "candidate_blocked_by_rules")
                answer, judge_score = "OK", 3
                report = evaluator.evaluate(candidate["id"], suite_path, replay, model_settings={}, judge_model=judge)
                self.assertEqual(report["decision"], "baseline_retained")
                judge_score = 2
                report = evaluator.evaluate(candidate["id"], suite_path, replay, model_settings={}, judge_model=judge)
                self.assertEqual(report["decision"], "candidate_blocked_by_judge")

                def bad_judge(_, observe):
                    observe(None)
                    return {"role": "assistant", "content": '{"score":true,"reason":"bad"}'}

                report = evaluator.evaluate(candidate["id"], suite_path, replay, model_settings={}, judge_model=bad_judge)
                self.assertEqual(report["decision"], "incomplete")
                self.assertEqual(report["rows"][0]["judge"]["status"], "error")
                self.assertIn('"score":true', report["rows"][0]["judge"]["output"])
                self.assertEqual(report["rows"][1]["status"], "not_run")
                self.assertIsNone(report["summary"]["baseline"]["mean_judge_score"])

                # 开发集可比较，但不能生成 champion。
                suite["split"] = "development"
                suite_path.write_text(json.dumps(suite), encoding="utf-8")
                def good_judge(_, observe):
                    observe(None)
                    return {"role": "assistant", "content": '{"score":4,"reason":"good"}'}
                answer = "OK"
                # 原版规则不通过，候选通过，两者语义均 4 分。
                def development_replay(messages, observe):
                    observe(None)
                    return {"role": "assistant", "content": "OK" if "EXPANDED_METHOD" in messages[0]["content"] else "baseline"}
                report = evaluator.evaluate(candidate["id"], suite_path, development_replay, model_settings={}, judge_model=good_judge)
                self.assertEqual(report["decision"], "candidate_selected")
                self.assertFalse(report["selection"]["champion_eligible"])

                with patch("personal_agent.main.get_model_message", side_effect=lambda *_, on_usage, **__: (on_usage({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}) or {"role": "assistant", "content": "OK"})) as request, patch("builtins.input", side_effect=[f"/skill-eval-judge {candidate['id']} {suite_path}", "exit"]), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    run_repl(prices=TokenPrices(Decimal(1), Decimal(1)), max_cost=Decimal("0.000002"))
                request.assert_called_once()
                budget_report = next(item for item in evaluator.list_reports() if "预算" in item["rows"][0]["judge"].get("error", {}).get("message", ""))
                self.assertEqual(budget_report["decision"], "incomplete")
                self.assertFalse(budget_report["rows"][0]["judge"]["logical_call_started"])
                self.assertEqual(evaluator.list_champions(), [])
                self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
