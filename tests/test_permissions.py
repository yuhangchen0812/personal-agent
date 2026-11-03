"""五种模式和规则优先级的三个核心测试。"""

import unittest

from personal_agent.permissions import check_permission


class PermissionTests(unittest.TestCase):
    def test_mode_table(self):
        tools = ("read_file", "write_file", "edit_file", "run_shell")
        expected = {
            "default": ("allow", "confirm", "confirm", "confirm"),
            "plan": ("allow", "deny", "deny", "deny"),
            "acceptEdits": ("allow", "allow", "allow", "confirm"),
            "dontAsk": ("allow", "deny", "deny", "deny"),
            "bypassPermissions": ("allow", "allow", "allow", "allow"),
        }
        for mode, actions in expected.items():
            with self.subTest(mode=mode):
                self.assertEqual(tuple(check_permission(tool, mode)["action"] for tool in tools), actions)

    def test_rules_and_plan_priority(self):
        self.assertEqual(check_permission("write_file", allow=["write_file"])["action"], "allow")
        self.assertEqual(check_permission("write_file", "dontAsk", allow=["write_file"])["action"], "allow")
        self.assertEqual(check_permission("write_file", allow=["write_file"], deny=["write_file"])["action"], "deny")
        self.assertEqual(check_permission("write_file", "plan", allow=["write_file"])["action"], "deny")
        self.assertEqual(check_permission("write_file", "bypassPermissions", deny=["write_file"])["action"], "deny")
        self.assertEqual(check_permission("read_file", deny=["read_file"])["action"], "deny")

    def test_unknown_input_is_denied(self):
        self.assertEqual(check_permission("unknown", "bypassPermissions")["action"], "deny")
        self.assertEqual(check_permission("read_file", "invalid-mode")["action"], "deny")
        self.assertEqual(check_permission("read_file", allow=["typo"])["action"], "deny")
