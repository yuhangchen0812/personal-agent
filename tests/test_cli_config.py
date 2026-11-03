"""两条 CLI 配置主流程；真实 SDK/固定 HTTP，文件、线程与权限真实。"""

import unittest

from tests.support.verify_cli_config import verify_inheritance, verify_protocol_and_permissions


class CliConfigurationTests(unittest.TestCase):
    def test_cli_scope_reaches_child_background_and_evaluation(self):
        report = verify_inheritance()
        self.assertEqual(report["status"], "passed")
        self.assertEqual(len(report["requests"]), 7)

    def test_dual_protocol_streaming_permissions_and_early_errors(self):
        report = verify_protocol_and_permissions()
        self.assertEqual(report["status"], "passed")
        self.assertEqual(len(report["permissions"]), 4)


if __name__ == "__main__":
    unittest.main()
