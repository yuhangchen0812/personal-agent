"""受控修复的实际文件、pytest、故障恢复和跨进程 CLI 流程。"""

import unittest

from tests.support.verify_ci_repair import verify_cli, verify_guards, verify_recovery, verify_workflow


class CiRepairTests(unittest.TestCase):
    def test_apply_and_rollback_with_actual_pytest(self):
        self.assertEqual(verify_workflow()["status"], "passed")

    def test_partial_adoption_evidence_versions_and_foreign_edits(self):
        self.assertEqual(verify_guards()["status"], "passed")

    def test_write_failure_cancel_receipt_failure_and_process_recovery(self):
        self.assertEqual(verify_recovery()["status"], "passed")

    def test_cli_from_cmd_and_powershell_with_relative_paths(self):
        self.assertEqual(verify_cli()["status"], "passed")


if __name__ == "__main__":
    unittest.main()
