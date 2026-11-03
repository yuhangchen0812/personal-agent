"""一条委派联合流程：固定模型，真实文件、父级审批和新进程恢复。"""

import unittest

from tests.support.verify_delegation_delivery import run_suite


class DelegationDeliveryTests(unittest.TestCase):
    def test_plan_general_custom_and_fork_with_restore(self):
        report = run_suite()
        self.assertEqual(report["status"], "passed")
        self.assertTrue(all(case["checks"]["parent_usage_includes_every_child_request"] for case in report["cases"].values()))
        self.assertEqual(report["restore"]["status"], "passed")


if __name__ == "__main__":
    unittest.main()
