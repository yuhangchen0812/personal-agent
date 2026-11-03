"""一个发现/列表流程：坏文件、重名、即时更新与纯本地命令。"""

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from personal_agent.agent_config import discover_project_agents
from personal_agent.main import run_repl
from personal_agent.session import Session
from personal_agent.skills import create_project_skill
from personal_agent.usage import TokenPrices


class AgentDiscoverySmokeTest(unittest.TestCase):
    def test_project_discovery_and_listing_preserve_history_and_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            directory = root / ".personal/agents"
            expected = {}

            def write_config(filename, content):
                path = directory / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
                expected[path] = path.read_bytes()
                return path

            output, errors = io.StringIO(), io.StringIO()
            with (
                patch("personal_agent.agent_config.PROJECT_ROOT", root),
                patch("personal_agent.agent_config.USER_AGENTS_ROOT", root / "user-agents"),
                patch("personal_agent.skills.PROJECT_ROOT", root),
                patch("personal_agent.skills.USER_SKILLS_ROOT", root / "user-skills"),
                patch("personal_agent.session.PROJECT_ROOT", root),
                patch("personal_agent.main.get_model_message", side_effect=AssertionError("本地列表不能请求模型")) as request,
                redirect_stdout(output), redirect_stderr(errors),
            ):
                self.assertEqual(discover_project_agents(), ([], []))
                self.assertFalse(directory.exists())
                # 空目录也应正常返回空，不创建或注册配置。
                directory.mkdir(parents=True)
                self.assertEqual(discover_project_agents(), ([], []))
                reader = write_config("different-file.md", "---\nname: code-reader\ndescription: 旧描述\nallowed-tools: read_file\n---\nBODY_NOT_LISTED")
                write_config("文本助手.md", "只回答问题")
                write_config("empty.MD", "---\nallowed-tools: []\n---\n不使用工具")
                write_config("general.md", "自定义同名配置，不应覆盖内置执行")
                write_config("dupe-a.md", "---\nname: duplicate\n---\n重复一")
                write_config("dupe-b.md", "---\nname: duplicate\n---\n重复二")
                write_config("broken.md", "---\nname: broken\n未闭合")
                write_config("ignored.txt", "不扫描")
                write_config("nested/deeper.md", "不递归扫描")
                (directory / "folder.md").mkdir()
                definitions, warnings = discover_project_agents()
                self.assertEqual([item.name for item in definitions], ["code-reader", "empty", "general", "文本助手"])
                self.assertEqual(len(warnings), 2)
                self.assertTrue(any("未闭合" in warning for warning in warnings))
                duplicate = next(warning for warning in warnings if "Agent 名重复" in warning)
                self.assertIn("dupe-a.md", duplicate)
                self.assertIn("dupe-b.md", duplicate)
                with self.assertRaises(ValueError):
                    create_project_skill("agents", "不占用命令", "正文")

                original = [{"role": "user", "content": "已有问题"}, {"role": "assistant", "content": "已有回答"}]
                session = Session("agent_listing")
                session.create()
                session.save(original)

                def inputs():
                    yield "/agents extra"
                    yield "/AGENTS"
                    request.assert_not_called()
                    reader.write_text("---\nname: code-reader\ndescription: 新描述\nallowed-tools: []\n---\nBODY_NOT_LISTED", encoding="utf-8")
                    expected[reader] = reader.read_bytes()
                    yield "/agents"
                    yield "/usage"
                    yield "exit"

                with patch("builtins.input", side_effect=inputs()):
                    history = run_repl(session_name="agent_listing", resume=True, prices=TokenPrices(Decimal(2), Decimal(8)), max_cost=Decimal(0))
                self.assertEqual(history, original)
                self.assertEqual(session.load(), original)
                request.assert_not_called()
                for path, data in expected.items():
                    self.assertEqual(path.read_bytes(), data)
                # 根路径是文件时明确报错，不当作空目录。
                other_root = root / "other-project"
                invalid = other_root / ".personal/agents"
                invalid.parent.mkdir(parents=True)
                invalid.write_text("原内容", encoding="utf-8")
                with patch("personal_agent.agent_config.PROJECT_ROOT", other_root):
                    with self.assertRaisesRegex(ValueError, "必须是目录"):
                        discover_project_agents()
                self.assertEqual(invalid.read_text(encoding="utf-8"), "原内容")

            self.assertIn("用法：/agents", output.getvalue())
            self.assertIn("code-reader [可委派] 旧描述", output.getvalue())
            self.assertIn("code-reader [可委派] 新描述", output.getvalue())
            self.assertIn("无工具（显式 []）", output.getvalue())
            self.assertIn("未声明（使用 general 能力上限，仍受父级权限约束）", output.getvalue())
            self.assertIn("general [与内置类型同名，内置优先，未启用此配置]", output.getvalue())
            self.assertIn("逻辑调用 0 次", output.getvalue())
            self.assertNotIn("BODY_NOT_LISTED", output.getvalue())
            self.assertNotIn("本轮请求失败", errors.getvalue())
            self.assertIn("Agent 名重复", errors.getvalue())
            self.assertIn("未闭合", errors.getvalue())
