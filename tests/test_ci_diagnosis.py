"""诊断证据、候选差异和已知演示复验的两条流程，模型固定。"""

import unittest

from codex.verify_ci_diagnosis import verify_guards, verify_workflow


class CiDiagnosisTests(unittest.TestCase):
    def test_observed_source_evidence_proposal_and_isolated_demo_retest(self):
        self.assertEqual(verify_workflow()["status"], "passed")

    def test_read_only_tools_citations_test_preservation_budget_cancel_and_versions(self):
        self.assertEqual(verify_guards()["status"], "passed")


if __name__ == "__main__":
    unittest.main()
