import unittest

from codex.verify_evidence_limits import verify_guards, verify_workflow


class EvidenceLimitsTests(unittest.TestCase):
    def test_partial_scope_missing_inputs_retest_explicit_adoption_and_rollback(self):
        self.assertEqual(verify_workflow()["status"], "passed")

    def test_contradictions_missing_source_and_false_supported_regression(self):
        self.assertEqual(verify_guards()["status"], "passed")


if __name__ == "__main__":
    unittest.main()
