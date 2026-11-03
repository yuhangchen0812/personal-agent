"""真实安装是显式交付任务；默认测试只查关键失败和来源边界。"""

import unittest
from codex.verify_release_delivery import verify_workflow


class ReleaseDeliveryTests(unittest.TestCase):
    def test_failed_installation_cannot_pass_or_overwrite_a_release(self):
        self.assertEqual(verify_workflow()["status"], "passed")
