import unittest

from codex.verify_rule_evidence import verify_guards, verify_workflow


class RuleEvidenceTests(unittest.TestCase):
    def test_explicit_rules_scoped_fix_independent_retest_adoption_and_rollback(self):
        self.assertEqual(verify_workflow()["status"], "passed")

    def test_unread_unbound_or_stale_rules_cannot_generate_or_adopt_candidates(self):
        self.assertEqual(verify_guards()["status"], "passed")


if __name__ == "__main__":
    unittest.main()
