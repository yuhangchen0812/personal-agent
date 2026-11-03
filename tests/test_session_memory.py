"""两条独立会话记忆流程：新进程/权限与发布失败/完整性/格式兼容。"""

import unittest

from codex.verify_session_memory import verify_failures_and_compatibility, verify_workflow


class SessionMemoryTests(unittest.TestCase):
    def test_structured_memory_context_and_new_process_restore(self):
        report = verify_workflow()
        self.assertEqual(report["status"], "passed")
        self.assertFalse(report["child_restore"]["historical_tools_replayed"])

    def test_failure_publication_integrity_budget_and_provider_compatibility(self):
        self.assertEqual(verify_failures_and_compatibility()["status"], "passed")


if __name__ == "__main__":
    unittest.main()
