"""显式测试配置、来源绑定和受限子进程的两条主流程。"""

import unittest

from tests.support.verify_configured_tests import verify_guards, verify_workflow


class ConfiguredTests(unittest.TestCase):
    def test_external_project_cli_baseline_candidate_adoption_and_rollback(self):
        self.assertEqual(verify_workflow()["status"], "passed")

    def test_missing_coverage_config_versions_and_process_cleanup(self):
        self.assertEqual(verify_guards()["status"], "passed")


if __name__ == "__main__":
    unittest.main()
