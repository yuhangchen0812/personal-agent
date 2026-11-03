import unittest

from codex.verify_diagnosis_history import verify_workflow


class DiagnosisHistoryTests(unittest.TestCase):
    def test_history_rounds_receipts_cli_unknowns_and_stale_or_tampered_evidence(self):
        self.assertEqual(verify_workflow()["status"], "passed")
