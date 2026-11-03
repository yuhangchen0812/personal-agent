"""一条 inline 主链路：字面替换、原权限、会话快照和关键错误。"""

import copy
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from personal_agent.main import run_repl
from personal_agent.session import Session
from personal_agent.skills import load_skill, prepare_inline_skill, resolve_skill_prompt


class InlineSkillSmokeTest(unittest.TestCase):
    def test_inline_request_uses_snapshot_and_existing_execution_boundary(self):
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
            {"role": "assistant", "content": "检查完成，写入已被权限拒绝。"},
            ValueError("模拟模型失败"),
            {"role": "assistant", "content": "普通追问完成。"},
        ])

        def model(messages, *_, **__):
            requests.append(copy.deepcopy(messages))
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return reply

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "sample skills/review/SKILL.md"
            path.parent.mkdir(parents=True)
            (root / "source.txt").write_text("实际待检查内容", encoding="utf-8")
            path.write_text(
                "---\nname: review\nallowed-tools: [\"read_file\", \"write_file\"]\n---\n"
                "先读取再检查：$ARGUMENTS\n另一种写法：${ARGUMENTS}\n目录：${CLAUDE_SKILL_DIR}",
                encoding="utf-8",
            )
            private = root / "private/SKILL.md"
            private.parent.mkdir()
            private.write_text("---\nuser-invocable: false\n---\n内部方法", encoding="utf-8")
            fork = root / "fork/SKILL.md"
            fork.parent.mkdir()
            fork.write_text("---\ncontext: fork\n---\n子任务方法", encoding="utf-8")
            arguments = r"source.txt C:\new\1.txt ${CLAUDE_SKILL_DIR} $ARGUMENTS"
            command = f'/skill "sample skills/review/SKILL.md" {arguments}'

            def inputs():
                yield "/skill"
                yield "/skill missing/SKILL.md"
                yield "/skill private/SKILL.md"
                yield command
                saved = Session("skill_inline").load()
                self.assertIn(command, saved[0]["content"])
                self.assertIn(f"目录：{path.parent}", saved[0]["content"])
                yield command  # 模型失败后不能留下半轮。
                yield "普通追问"
                yield "exit"

            output, errors = io.StringIO(), io.StringIO()
            with (
                patch("personal_agent.skills.PROJECT_ROOT", root),
                patch("personal_agent.tools.PROJECT_ROOT", root),
                patch("personal_agent.session.PROJECT_ROOT", root),
                patch("personal_agent.main.get_model_message", side_effect=model),
                patch("personal_agent.main.confirm_in_terminal") as confirm,
                patch("builtins.input", side_effect=inputs()),
                redirect_stdout(output), redirect_stderr(errors),
            ):
                skill = load_skill(path)
                rendered = resolve_skill_prompt(skill, arguments)
                self.assertIn(f"先读取再检查：{arguments}", rendered)
                self.assertIn(f"另一种写法：{arguments}", rendered)
                self.assertIn("先读取再检查：\n", resolve_skill_prompt(skill))
                self.assertIn("$ARGUMENTS", skill.prompt_template)
                with self.assertRaisesRegex(ValueError, "fork"):
                    prepare_inline_skill("/skill fork/SKILL.md")
                history = run_repl(session_name="skill_inline", with_tools=True, deny=["write_file"])
                self.assertEqual(Session("skill_inline").load(), history)
                confirm.assert_not_called()

                # 恢复使用存档快照，不重新读取已被修改的技能正文。
                path.write_text("方法已修改", encoding="utf-8")
                with patch("builtins.input", side_effect=["exit"]):
                    restored = run_repl(session_name="skill_inline", resume=True)
                self.assertEqual(restored, history)

            self.assertEqual(len(requests), 4)  # 无效调用没有模型请求。
            self.assertEqual(requests[0][-1]["content"], history[0]["content"])
            self.assertEqual(requests[1][1]["content"], history[0]["content"])
            self.assertIn("实际待检查内容", requests[1][-2]["content"])
            self.assertIn("deny", requests[1][-1]["content"])
            self.assertFalse((root / "playground/result.txt").exists())
            self.assertEqual(len(history), 7)
            self.assertEqual(history[-2], {"role": "user", "content": "普通追问"})
            self.assertEqual(sum(m["role"] == "user" for m in history), 2)
            for expected in ("用法", "找不到", "user-invocable", "模拟模型失败"):
                self.assertIn(expected, errors.getvalue())
