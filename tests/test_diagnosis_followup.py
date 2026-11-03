import unittest

from tests.support.verify_diagnosis_followup import verify_guards, verify_workflow


class DiagnosisFollowupTests(unittest.TestCase):
    def test_parent_bound_generation_comparison_verified_tests_and_rollback(self):
        self.assertEqual(verify_workflow()["status"], "passed")

    def test_stale_unrelated_cyclic_or_late_parent_and_forged_comparison(self):
        self.assertEqual(verify_guards()["status"], "passed")


if __name__ == "__main__":
    unittest.main()
