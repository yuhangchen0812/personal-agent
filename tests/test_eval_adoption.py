"""公开/独立回归绑定采纳的两条完整组件流程。"""

import unittest

from tests.support.verify_eval_adoption import verify_guards, verify_workflow


class EvalAdoptionTests(unittest.TestCase):
    def test_full_and_partial_receipts_cli_apply_sequential_cases_and_rollback(self):
        self.assertEqual(verify_workflow()["status"], "passed")

    def test_regression_public_bypass_stale_evidence_and_locked_recheck(self):
        self.assertEqual(verify_guards()["status"], "passed")


if __name__ == "__main__":
    unittest.main()
