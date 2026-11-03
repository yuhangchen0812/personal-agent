"""诊断的原生结构约束、终结隔离和失败证据两条主流程。"""

import unittest

from codex.verify_structured_diagnosis import verify_guards, verify_workflow


class StructuredDiagnosisTests(unittest.TestCase):
    def test_native_two_protocol_schema_then_retest_adopt_and_rollback(self):
        self.assertEqual(verify_workflow()["status"], "passed")

    def test_format_evidence_refusal_truncation_budget_and_no_downgrade(self):
        self.assertEqual(verify_guards()["status"], "passed")


if __name__ == "__main__":
    unittest.main()
