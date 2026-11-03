import unittest

from codex.verify_project_import import verify_workflow


class ProjectImportTests(unittest.TestCase):
    def test_project_junit_full_workflow_public_only_and_stale_inputs(self):
        self.assertEqual(verify_workflow()["status"], "passed")
