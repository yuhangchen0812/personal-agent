"""容器交付与 CLI 停止信号共用验证，不重复付费模型调用。"""

import unittest
from codex.verify_container_delivery import verify_workflow


class ContainerDeliveryTests(unittest.TestCase):
    def test_cli_stop_persistence_and_engine_failure_lifecycle(self):
        self.assertEqual(verify_workflow()["status"], "passed")
