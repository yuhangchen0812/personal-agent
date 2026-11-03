"""两条缓存主流程：读取/索引复用与 CLI 失效、权限和工程边界。"""

import unittest

from codex.verify_skill_cache import verify_cli_and_boundaries, verify_reuse_and_external_changes


class SkillCacheTests(unittest.TestCase):
    def test_reuse_preserves_scores_and_detects_external_file_changes(self):
        report = verify_reuse_and_external_changes()
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["comparison"]["with_cache"]["skill_file_reads"], 3)

    def test_cli_mutations_refresh_execution_and_scope_boundaries(self):
        report = verify_cli_and_boundaries()
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["model_callbacks"], 1)


if __name__ == "__main__":
    unittest.main()
