import unittest

from tests.support.verify_repair_workflow import verify_workflow


class RepairWorkflowTests(unittest.TestCase):
    def test_complete_task_recovery_idempotency_regression_and_source_versions(self):
        self.assertEqual(verify_workflow()["status"], "passed")
