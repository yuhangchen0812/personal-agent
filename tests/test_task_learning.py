"""应用反馈到显式采纳的两条定向流程，不请求真实模型。"""

import unittest

from tests.support.verify_task_learning import verify_guards, verify_workflow


class TaskLearningTests(unittest.TestCase):
    def test_feedback_freeze_paired_replay_explicit_adoption_and_following_run(self):
        self.assertEqual(verify_workflow()["status"], "passed")

    def test_source_split_budget_cancel_version_and_artifact_guards(self):
        self.assertEqual(verify_guards()["status"], "passed")


if __name__ == "__main__":
    unittest.main()
