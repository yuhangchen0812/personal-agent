"""一条跨模块主流程；固定模型，真实文件、CLI 和新进程。"""

import unittest

from codex.verify_runtime_acceptance import verify_joint


class RuntimeAcceptanceTests(unittest.TestCase):
    def test_memory_cache_restore_and_permissions_work_together(self):
        report = verify_joint()
        self.assertEqual(report["status"], "passed")
        self.assertFalse(report["restore"]["historical_tools_replayed"])
        self.assertTrue(report["restore"]["explicit_deny_and_restored_plan_block_before_handlers"])


if __name__ == "__main__":
    unittest.main()
