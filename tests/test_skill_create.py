"""一个手动创建流程：不覆盖、立即发现/调用与写入失败清理。"""

import copy
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from personal_agent.main import run_repl
from personal_agent.session import Session
from personal_agent.skills import create_project_skill, load_inline_skill, load_skill


class SkillCreateSmokeTest(unittest.TestCase):
    def test_manual_creation_roundtrips_and_preserves_existing_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            root, user_root = base / "project", base / "home/.personal/skills"
            path = root / ".personal/skills/explain-flow/SKILL.md"
            description = '解释 "调用流程"'
            body = "任务：$ARGUMENTS。先读取代码，再按 输入 | 处理 | 输出 解释。"
            command = f"/skill-create explain-flow | {description} | 需要解释函数调用时 | {body}"
            requests = []

            def model(messages, *_, **__):
                requests.append(copy.deepcopy(messages))
                return {"role": "assistant", "content": "按新方法完成讲解。"}

            output, errors = io.StringIO(), io.StringIO()
            with (
                patch("personal_agent.skills.PROJECT_ROOT", root),
                patch("personal_agent.skills.USER_SKILLS_ROOT", user_root),
                patch("personal_agent.tools.PROJECT_ROOT", root),
                patch("personal_agent.session.PROJECT_ROOT", root),
                patch("personal_agent.main.get_model_message", side_effect=model) as request,
                redirect_stdout(output), redirect_stderr(errors),
            ):
                for name in ("../escape", "memory", "CON"):
                    with self.assertRaises(ValueError):
                        create_project_skill(name, "说明", "正文")
                self.assertFalse((root / ".personal/skills").exists())
                user = user_root / "custom-folder/SKILL.md"
                user.parent.mkdir(parents=True)
                user.write_text("---\nname: shared-name\n---\n用户定义", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "名称已存在"):
                    create_project_skill("shared-name", "说明", "正文")

                def inputs():
                    yield "/skill-create"
                    yield command
                    request.assert_not_called()
                    skill = load_skill(path)
                    self.assertEqual(skill.description, description)
                    self.assertEqual(skill.when_to_use, "需要解释函数调用时")
                    self.assertEqual(skill.prompt_template, body)
                    self.assertEqual((skill.context, skill.user_invocable, skill.source), ("inline", True, "project"))
                    before = path.read_bytes()
                    yield command.replace(body, "不应覆盖旧正文")
                    self.assertEqual(path.read_bytes(), before)
                    yield "/skills"
                    yield "/skills search 解释调用流程"
                    request.assert_not_called()
                    yield "/explain-flow source.txt"
                    yield "exit"

                with patch("builtins.input", side_effect=inputs()):
                    history = run_repl(session_name="skill_create", with_tools=True, permission_mode="plan")
                self.assertEqual(Session("skill_create").load(), history)
                self.assertEqual(len(requests), 1)
                self.assertIn("任务：source.txt", requests[0][-1]["content"])
                self.assertIn("输入 | 处理 | 输出", requests[0][-1]["content"])
                self.assertIn("任务：other.txt", load_inline_skill("explain-flow", "other.txt"))

                # 同名检查使用元信息 name，不依赖目录名。
                project = root / ".personal/skills/other-folder/SKILL.md"
                project.parent.mkdir()
                project.write_text("---\nname: occupied-name\n---\n已有项目定义", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "名称已存在"):
                    create_project_skill("occupied-name", "说明", "正文")
                empty = root / ".personal/skills/empty-folder"
                empty.mkdir()
                with self.assertRaisesRegex(ValueError, "目录已存在"):
                    create_project_skill("empty-folder", "说明", "正文")
                self.assertTrue(empty.is_dir())

                original_open = Path.open

                def fail_write(target, mode="r", *args, **kwargs):
                    if mode == "x":
                        raise OSError("模拟写入失败")
                    return original_open(target, mode, *args, **kwargs)

                before = path.read_bytes()
                with patch.object(Path, "open", new=fail_write):
                    with self.assertRaisesRegex(OSError, "模拟写入失败"):
                        create_project_skill("broken-write", "说明", "正文")
                self.assertFalse((root / ".personal/skills/broken-write").exists())
                self.assertEqual(path.read_bytes(), before)

            self.assertEqual(len(history), 2)
            self.assertTrue(history[0]["content"].startswith("技能调用：/explain-flow"))
            self.assertNotIn("/skill-create", str(history))
            self.assertIn("用法：/skill-create", output.getvalue())
            self.assertIn("已创建项目技能", output.getvalue())
            self.assertIn("/explain-flow [可调用]", output.getvalue())
            self.assertIn("1. explain-flow 分数=", output.getvalue())
            self.assertIn("名称已存在", errors.getvalue())
