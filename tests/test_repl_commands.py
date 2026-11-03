"""两条 REPL 入口主流程：联合成功路径与核心权限/预算错误。"""

import unittest

from tests.support.verify_repl_commands import verify_guards, verify_workflow


class ReplCommandTests(unittest.TestCase):
    def test_plan_rating_and_immediate_extraction_use_existing_runtime(self):
        report = verify_workflow()
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["pending_candidates"], 2)

    def test_permissions_budget_text_mode_and_plain_single_prompt(self):
        report = verify_guards()
        self.assertEqual(report["status"], "passed")
        self.assertTrue(all(report["checks"].values()))


if __name__ == "__main__":
    unittest.main()
