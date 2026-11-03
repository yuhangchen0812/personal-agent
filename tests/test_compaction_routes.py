"""两条主要流程，验证统一入口、停用、兼容与未完成任务。"""

import unittest

from codex.verify_compaction_routes import verify_compatibility, verify_routes


class CompactionRoutesTests(unittest.TestCase):
    def test_manual_auto_tool_share_memory_and_respect_off(self):
        report = verify_routes()
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["memory_requests"], 3)

    def test_old_text_anonymous_and_failed_task_compatibility(self):
        self.assertEqual(verify_compatibility()["status"], "passed")


if __name__ == "__main__":
    unittest.main()
