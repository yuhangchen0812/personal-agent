"""只保留一条跨模块联合验收；固定模型，实际运行工具与 MCP。"""

import unittest

from tests.support.verify_delivery import run_joint


class DeliveryTests(unittest.TestCase):
    def test_integrated_workflow_preserves_permissions_versions_and_session(self):
        report = run_joint()
        self.assertEqual(report["status"], "passed")
        self.assertTrue(all(report["stages"].values()))
        self.assertEqual(report["evaluation"]["decision"], "tie_on_tasks")
