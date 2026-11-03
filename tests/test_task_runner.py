"""文件任务包两条主流程；不发真实模型请求。"""

import unittest

from tests.support.verify_ci_tasks import verify_failures, verify_workflow


class TaskRunnerTests(unittest.TestCase):
    def test_development_holdout_real_artifacts_and_isolation(self):
        self.assertEqual(verify_workflow()["status"], "passed")

    def test_completion_claim_permissions_budget_cancel_and_split_guards(self):
        self.assertEqual(verify_failures()["status"], "passed")


if __name__ == "__main__":
    unittest.main()
