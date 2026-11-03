import unittest

from codex.verify_skill_learning import verify_workflow


class SkillLearningTests(unittest.TestCase):
    def test_continuous_learning_and_bound_task_use(self):
        self.assertEqual(verify_workflow()["status"], "passed")
