"""一个项目技能发现、名称调用与即时更新的最小流程。"""

import copy
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from personal_agent.main import run_repl
from personal_agent.session import Session
from personal_agent.skills import discover_project_skills


class SkillDiscoverySmokeTest(unittest.TestCase):
    def test_project_listing_and_named_call_share_inline_execution(self):
        requests = []
        replies = iter([
            {"role": "assistant", "tool_calls": [{"id": "read", "type": "function", "function": {
                "name": "read_file", "arguments": '{"file_path":"source.txt"}',
            }}]},
            {"role": "assistant", "content": "代码已读取并检查。"},
            {"role": "assistant", "content": "已使用更新后的方法。"},
            {"role": "assistant", "content": "中文名称调用完成。"},
        ])

        def model(messages, *_, **__):
            requests.append(copy.deepcopy(messages))
            return next(replies)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            directory = root / ".personal/skills"

            def write_skill(folder, content):
                path = directory / folder / "SKILL.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
                return path

            output, errors = io.StringIO(), io.StringIO()
            with (
                patch("personal_agent.skills.PROJECT_ROOT", root),
                patch("personal_agent.skills.USER_SKILLS_ROOT", root / "absent-user/.personal/skills"),
                patch("personal_agent.tools.PROJECT_ROOT", root),
                patch("personal_agent.memory.PROJECT_ROOT", root),
                patch("personal_agent.session.PROJECT_ROOT", root),
                patch("personal_agent.main.get_model_message", side_effect=model),
                redirect_stdout(output), redirect_stderr(errors),
            ):
                self.assertEqual(discover_project_skills(), ([], []))
                self.assertFalse(directory.exists())
                review = write_skill("different-folder", "---\nname: review\ndescription: 核心检查\n---\n检查参数：$ARGUMENTS")
                write_skill("编码检查", "编码参数：${ARGUMENTS}")
                write_skill("dupe-a", "---\nname: duplicate\n---\n重复方法一")
                write_skill("dupe-b", "---\nname: duplicate\n---\n重复方法二")
                write_skill("broken", "---\nname: broken\n未闭合")
                write_skill("memory", "不应覆盖内置命令")
                write_skill("nested/deeper", "深层方法不应被发现")
                (directory / "loose.md").write_text("单文件不应被发现", encoding="utf-8")
                (root / "source.txt").write_text("实际源文件", encoding="utf-8")
                skills, warnings = discover_project_skills()
                self.assertEqual({skill.name for skill in skills}, {"review", "编码检查", "memory"})
                self.assertTrue(any("技能名重复" in warning for warning in warnings))
                self.assertTrue(any("未闭合" in warning for warning in warnings))

                def inputs():
                    yield "/skills"
                    yield "/skills extra"
                    yield "/missing 参数"
                    yield "/duplicate 参数"
                    yield "/memory"  # 内置命令优先，不执行同名技能。
                    yield "/review source.txt"
                    self.assertIn("技能调用：/review source.txt", Session("named_skill").load()[0]["content"])
                    review.write_text("---\nname: review\n---\n更新方法：$ARGUMENTS", encoding="utf-8")
                    yield "/review 第二次"
                    yield "/编码检查"
                    yield "exit"

                with patch("builtins.input", side_effect=inputs()):
                    history = run_repl(session_name="named_skill", with_tools=True)
                self.assertEqual(Session("named_skill").load(), history)

            self.assertEqual(len(requests), 4)
            self.assertIn("检查参数：source.txt", requests[0][-1]["content"])
            self.assertIn("实际源文件", requests[1][-1]["content"])
            self.assertIn("更新方法：第二次", requests[2][-1]["content"])
            self.assertTrue(requests[3][-1]["content"].endswith("编码参数："))
            self.assertIn("/review [可调用] 核心检查", output.getvalue())
            self.assertIn("与内置命令同名", output.getvalue())
            self.assertIn("/skills；/skills search 查询", output.getvalue())
            self.assertIn("未知技能", errors.getvalue())
            self.assertEqual(sum(message["role"] == "user" for message in history), 3)
