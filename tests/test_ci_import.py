"""CI 原始目录导入、运行期契约和批量执行的两个流程；不请求模型。"""

import unittest

from tests.support.verify_ci_import import verify_guards, verify_workflow


class CiImportTests(unittest.TestCase):
    def test_single_batch_import_raw_bytes_provenance_and_runtime_contract(self):
        self.assertEqual(verify_workflow()["status"], "passed")

    def test_artifact_evidence_source_changes_permissions_budget_cancel_and_preflight(self):
        self.assertEqual(verify_guards()["status"], "passed")


if __name__ == "__main__":
    unittest.main()
