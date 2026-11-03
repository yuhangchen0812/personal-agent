"""一条用户/项目技能合并链路，验证优先级、来源和既有执行权限。"""

import copy
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from personal_agent.main import run_repl
from personal_agent.session import Session
from personal_agent.skills import discover_skills, load_skill


class SkillScopesSmokeTest(unittest.TestCase):
    def test_user_override_explicit_project_and_invalid_user_fallback(self):
        requests = []
        replies = iter([
            {"role": "assistant", "tool_calls": [
                {"id": "read", "type": "function", "function": {
                    "name": "read_file", "arguments": '{"file_path":"source.txt"}',
                }},
                {"id": "write", "type": "function", "function": {
                    "name": "write_file", "arguments": '{"file_path":"playground/result.txt","content":"不应写入"}',
                }},
            ]},
            {"role": "assistant", "content": "使用用户方法检查完成，写入已被拒绝。"},
            {"role": "assistant", "content": "显式使用项目方法。"},
            {"role": "assistant", "content": "使用有效项目定义继续。"},
        ])

        def model(messages, *_, **__):
            requests.append(copy.deepcopy(messages))
            return next(replies)

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            root = base / "project"
            user_root = base / "home/.personal/skills"
            project = root / ".personal/skills/project-folder/SKILL.md"
            user = user_root / "user-folder/SKILL.md"
            project.parent.mkdir(parents=True)
            user.parent.mkdir(parents=True)
            project.write_text("---\nname: review\n---\n项目方法：$ARGUMENTS", encoding="utf-8")
            user.write_text('---\nname: review\nallowed-tools: ["write_file"]\n---\n用户方法：$ARGUMENTS\n目录：${CLAUDE_SKILL_DIR}', encoding="utf-8")
            (root / "source.txt").write_text("项目中的实际内容", encoding="utf-8")
            outside = base / "home/notes/SKILL.md"
            outside.parent.mkdir()
            outside.write_text("普通主目录文件", encoding="utf-8")
            nested = user_root / "too/deep/SKILL.md"
            nested.parent.mkdir(parents=True)
            nested.write_text("嵌套用户技能", encoding="utf-8")

            output, errors = io.StringIO(), io.StringIO()
            with (
                patch("personal_agent.skills.PROJECT_ROOT", root),
                patch("personal_agent.skills.USER_SKILLS_ROOT", user_root),
                patch("personal_agent.tools.PROJECT_ROOT", root),
                patch("personal_agent.session.PROJECT_ROOT", root),
                patch("personal_agent.main.get_model_message", side_effect=model),
                patch("personal_agent.main.confirm_in_terminal") as confirm,
                redirect_stdout(output), redirect_stderr(errors),
            ):
                skills, warnings = discover_skills()
                self.assertFalse(warnings)
                self.assertEqual([(skill.name, skill.source, skill.file_path) for skill in skills], [("review", "user", user)])
                with self.assertRaisesRegex(ValueError, "范围"):
                    load_skill(outside)
                with self.assertRaisesRegex(ValueError, "用户技能只允许"):
                    load_skill(nested)
                with patch("personal_agent.skills.PROJECT_ROOT", base / "other-project"):
                    shared, _ = discover_skills()
                    self.assertEqual(shared[0].file_path, user)

                def inputs():
                    yield "/skills"
                    yield "/review 首次任务"
                    snapshot = Session("skill_scopes").load()[0]["content"]
                    self.assertIn("技能作用域：user", snapshot)
                    self.assertIn(f"技能来源：{user}", snapshot)
                    yield f'/skill "{project}" 显式项目任务'
                    user.write_text("---\nname: review\nuser-invocable: false\n---\n内部用户方法", encoding="utf-8")
                    yield "/review 不应回退"
                    user.write_text("---\nname: review\n未闭合", encoding="utf-8")
                    yield "/review 格式错误后采用项目定义"
                    yield "exit"

                with patch("builtins.input", side_effect=inputs()):
                    history = run_repl(session_name="skill_scopes", with_tools=True, deny=["write_file"])
                self.assertEqual(Session("skill_scopes").load(), history)
                confirm.assert_not_called()

            self.assertEqual(len(requests), 4)
            self.assertIn("用户方法：首次任务", requests[0][-1]["content"])
            self.assertIn(f"目录：{user.parent}", requests[0][-1]["content"])
            self.assertIn("项目中的实际内容", requests[1][-2]["content"])
            self.assertIn("deny", requests[1][-1]["content"])
            self.assertIn("技能作用域：project", requests[2][-1]["content"])
            self.assertIn("项目方法：显式项目任务", requests[2][-1]["content"])
            self.assertIn("项目方法：格式错误后采用项目定义", requests[3][-1]["content"])
            self.assertIn("来源：用户", output.getvalue())
            self.assertIn("user-invocable", errors.getvalue())
            self.assertIn("未闭合", errors.getvalue())
            self.assertFalse((root / "playground/result.txt").exists())
            self.assertEqual(sum(message["role"] == "user" for message in history), 3)
            self.assertIn("用户方法：首次任务", history[0]["content"])
