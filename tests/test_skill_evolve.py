"""一个手动演化流程：原始快照、立即复用、作用域及写入失败保护。"""

import copy
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from personal_agent.main import run_repl
from personal_agent.session import Session
from personal_agent.skills import create_project_skill, evolve_project_skill, load_inline_skill, load_skill


class SkillEvolveSmokeTest(unittest.TestCase):
    def test_manual_evolution_preserves_versions_and_updates_future_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            root, user_root = base / "project", base / "home/.personal/skills"
            path = root / ".personal/skills/custom-folder/SKILL.md"
            history_dir = root / ".personal/skill-history/explain-flow"
            path.parent.mkdir(parents=True)
            original = ("\ufeff---\r\nname: explain-flow\r\ndescription: 解释流程\r\n"
                        "allowed-tools: '[\"read_file\"]'\r\nuser-invocable: true\r\ncontext: inline\r\n"
                        "---\r\n任务：$ARGUMENTS。读取后解释。\r\n").encode("utf-8")
            path.write_bytes(original)
            lesson = "解释 frontmatter 时，分别说明原始文本和解析后值的类型。"
            command = f"/skill-evolve explain-flow {lesson}"
            requests = []

            def model(messages, *_, **__):
                requests.append(copy.deepcopy(messages))
                return {"role": "assistant", "content": "固定讲解回复。"}

            output, errors = io.StringIO(), io.StringIO()
            with (
                patch("personal_agent.skills.PROJECT_ROOT", root),
                patch("personal_agent.skills.USER_SKILLS_ROOT", user_root),
                patch("personal_agent.tools.PROJECT_ROOT", root),
                patch("personal_agent.session.PROJECT_ROOT", root),
                patch("personal_agent.main.get_model_message", side_effect=model),
                redirect_stdout(output), redirect_stderr(errors),
            ):
                def inputs():
                    yield "/skill-evolve"
                    yield "/skill-evolve unknown 经验"
                    self.assertFalse(history_dir.exists())
                    yield "/explain-flow before.py"
                    self.assertEqual(len(requests), 1)
                    yield command
                    self.assertEqual(len(requests), 1)  # 维护命令本身不请求模型。
                    snapshots = list(history_dir.glob("*.md"))
                    self.assertEqual(len(snapshots), 1)
                    self.assertEqual(snapshots[0].read_bytes(), original)
                    self.assertTrue(path.read_bytes().startswith(original))
                    updated = load_skill(path)
                    self.assertEqual(updated.allowed_tools, ("read_file",))
                    self.assertIn(lesson, updated.prompt_template)
                    self.assertIn("$ARGUMENTS", updated.prompt_template)
                    self.assertIn(lesson, load_inline_skill("explain-flow", "tool.py"))
                    yield "/skills search 解析后值"
                    self.assertEqual(len(requests), 1)
                    yield "/explain-flow after.py"
                    yield "exit"

                with patch("builtins.input", side_effect=inputs()):
                    history = run_repl(session_name="skill_evolve", with_tools=True, permission_mode="plan")
                self.assertEqual(Session("skill_evolve").load(), history)
                self.assertEqual(len(history), 4)
                self.assertNotIn(lesson, history[0]["content"])
                self.assertIn(lesson, history[2]["content"])
                self.assertIn("任务：after.py", requests[1][-1]["content"])
                self.assertNotIn("/skill-evolve", str(history))

                before = path.read_bytes()
                _, second_snapshot = evolve_project_skill("explain-flow", "第二条经验")
                self.assertEqual(second_snapshot.read_bytes(), before)
                self.assertEqual(len(list(history_dir.glob("*.md"))), 2)

                user = user_root / "same-name/SKILL.md"
                user.parent.mkdir(parents=True)
                user.write_bytes(b"---\nname: explain-flow\n---\nuser skill")
                before = path.read_bytes()
                with self.assertRaisesRegex(ValueError, "用户技能"):
                    evolve_project_skill("explain-flow", "不能改隐藏的项目副本")
                self.assertEqual(path.read_bytes(), before)
                user.unlink()
                with self.assertRaises(ValueError):
                    evolve_project_skill("explain-flow", " ")
                with self.assertRaises(ValueError):
                    create_project_skill("skill-evolve", "说明", "正文")

                original_open = Path.open

                def fail_snapshot(target, mode="r", *args, **kwargs):
                    if mode == "xb":
                        raise OSError("模拟快照失败")
                    return original_open(target, mode, *args, **kwargs)

                with patch.object(Path, "open", new=fail_snapshot):
                    with self.assertRaisesRegex(OSError, "模拟快照失败"):
                        evolve_project_skill("explain-flow", "不能丢失原版本")
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(len(list(history_dir.glob("*.md"))), 2)

                with patch("personal_agent.skills.os.replace", side_effect=OSError("模拟替换失败")):
                    with self.assertRaisesRegex(OSError, "模拟替换失败"):
                        evolve_project_skill("explain-flow", "不能截断当前技能")
                self.assertEqual(path.read_bytes(), before)
                snapshots = list(history_dir.glob("*.md"))
                self.assertEqual(len(snapshots), 3)  # 完整快照在替换失败后仍保留。
                self.assertEqual(sum(item.read_bytes() == before for item in snapshots), 1)
                self.assertEqual(list(path.parent.glob(".evolve-*.tmp")), [])

            self.assertEqual(len(requests), 2)
            self.assertIn("用法：/skill-evolve", output.getvalue())
            self.assertIn("已演化项目技能", output.getvalue())
            self.assertIn("原版本快照：", output.getvalue())
            self.assertIn("1. explain-flow 分数=", output.getvalue())
            self.assertIn("找不到可演化的技能", errors.getvalue())
