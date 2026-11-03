"""一个单文件配置流程：样例、默认值、空工具列表及关键错误。"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from personal_agent.agent_config import load_agent_definition, main


class AgentConfigSmokeTest(unittest.TestCase):
    def test_single_definition_preserves_prompt_and_distinguishes_tool_defaults(self):
        output = io.StringIO()
        with redirect_stdout(output):
            main(["examples/agents/code-reader.md"])
        sample = json.loads(output.getvalue())
        self.assertEqual(sample["name"], "code-reader")
        self.assertEqual(sample["allowed_tools"], ["read_file", "list_files", "grep_search"])
        self.assertIn("输入、处理、输出", sample["system_prompt"])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / ".personal/agents/代码助手.md"
            path.parent.mkdir(parents=True)
            with patch("personal_agent.agent_config.PROJECT_ROOT", root):
                raw = "\ufeff# 角色\r\n    保留正文缩进与 $ARGUMENTS 字面内容。\r\n".encode("utf-8")
                path.write_bytes(raw)
                default = load_agent_definition(path)
                self.assertEqual(default.name, "代码助手")
                self.assertEqual((default.description, default.allowed_tools), ("", None))
                self.assertEqual(default.system_prompt, "# 角色\n    保留正文缩进与 $ARGUMENTS 字面内容。")
                self.assertEqual(path.read_bytes(), raw)
                path.write_text('---\nname: custom-reader\ndescription: "true"\nallowed-tools: read_file, grep_search, read_file\n---\n角色正文', encoding="utf-8")
                configured = load_agent_definition(path)
                self.assertEqual(configured.name, "custom-reader")
                self.assertEqual(configured.description, "true")
                self.assertEqual(configured.allowed_tools, ("read_file", "grep_search"))
                path.write_text("---\nallowed-tools: []\n---\n只回答，不请求工具", encoding="utf-8")
                self.assertEqual(load_agent_definition(path).allowed_tools, ())

                for content, message in [
                    ("---\nname: x\n正文", "未闭合"),
                    ("---\npermission-mode: bypassPermissions\n---\n正文", "暂不支持 Agent 字段"),
                    ("---\nallowed-tools: [\"read_file\", 7]\n---\n正文", "allowed-tools"),
                    ("---\nallowed-tools: agent\n---\n正文", "暂不支持这些工具"),
                    ("---\nallowed-tools: exit_plan_mode\n---\n正文", "暂不支持这些工具"),
                    ("---\nname: ../outside\n---\n正文", "Agent 名称"),
                    ("---\nname: x\n---\n  ", "提示词不能为空"),
                ]:
                    with self.subTest(message=message):
                        path.write_text(content, encoding="utf-8")
                        before = path.read_bytes()
                        with self.assertRaisesRegex(ValueError, message):
                            load_agent_definition(path)
                        self.assertEqual(path.read_bytes(), before)

                for invalid in (".env", "Reference/agents/x.md", ".personal/sessions/x.md", root.parent / "outside.md"):
                    with self.assertRaises(ValueError):
                        load_agent_definition(invalid)
                errors = io.StringIO()
                with redirect_stderr(errors):
                    with self.assertRaises(SystemExit) as error:
                        main([".personal/agents/missing.md"])
                self.assertEqual(error.exception.code, 1)
                self.assertIn("找不到 Agent 配置", errors.getvalue())
