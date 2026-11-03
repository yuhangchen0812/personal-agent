"""多样本评估的两条真实组件主流程，不发外部模型请求。"""

import unittest

from codex.verify_repair_eval import verify_guards, verify_workflow


class RepairEvalTests(unittest.TestCase):
    def test_isolated_cases_reviewed_retests_partial_abstention_and_private_regression(self):
        self.assertEqual(verify_workflow()["status"], "passed")

    def test_budget_denominators_format_versions_reviews_and_no_execution(self):
        self.assertEqual(verify_guards()["status"], "passed")


if __name__ == "__main__":
    unittest.main()
