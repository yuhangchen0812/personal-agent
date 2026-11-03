"""注册、执行和参数拦截的三个核心测试。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from personal_agent.tool_registry import (
    TOOL_ERROR_PREFIX,
    TOOL_REGISTRY,
    execute_tool,
    get_tool_definitions,
    validate_tool_arguments,
)


class ToolRegistryTests(unittest.TestCase):
    def test_definitions_and_defaults(self):
        definitions = get_tool_definitions()
        self.assertEqual([tool["name"] for tool in definitions], ["read_file", "list_files", "grep_search", "write_file", "edit_file", "run_shell", "enter_plan_mode", "exit_plan_mode", "compact_context", "skill", "tool_search", "agent"])
        self.assertNotIn("handler", json.dumps(definitions, ensure_ascii=False))
        arguments = {"file_path": "demo.txt"}
        self.assertEqual(
            validate_tool_arguments("read_file", arguments),
            {"file_path": "demo.txt", "offset": 1, "limit": 200},
        )
        self.assertEqual(arguments, {"file_path": "demo.txt"})
        self.assertIsNone(validate_tool_arguments("grep_search", {"pattern": "demo"})["include"])

    def test_all_three_tools_execute(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "demo.txt").write_text("中文示例\ndef greet(name):\n", encoding="utf-8")
            with patch("personal_agent.tools.PROJECT_ROOT", root):
                self.assertIn("   2 | def greet(name):", execute_tool("read_file", {"file_path": "demo.txt"}))
                self.assertEqual(execute_tool("list_files", {"pattern": "*.txt"}), "demo.txt")
                self.assertEqual(execute_tool("grep_search", {"pattern": "^def"}), "demo.txt:2:def greet(name):")

    def test_invalid_calls_do_not_execute_handler(self):
        handler = Mock()
        with patch.dict(TOOL_REGISTRY["read_file"], {"handler": handler}):
            cases = [
                ("unknown", {}),
                ("read_file", {}),
                ("read_file", {"file_path": "demo.txt", "offset": "2"}),
                ("read_file", {"file_path": "demo.txt", "offset": 0}),
                ("read_file", {"file_path": "demo.txt", "extra": True}),
            ]
            for name, arguments in cases:
                with self.subTest(name=name, arguments=arguments):
                    self.assertTrue(execute_tool(name, arguments).startswith(TOOL_ERROR_PREFIX))
        handler.assert_not_called()


if __name__ == "__main__":
    unittest.main()
