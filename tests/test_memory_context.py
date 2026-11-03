"""一个工具对话场景验证手动记忆进入请求、保持历史原样以及停用/清空。"""

import copy
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from personal_agent.main import run_repl
from personal_agent.memory import read_memory, save_memory
from personal_agent.session import Session


class MemoryContextSmokeTest(unittest.TestCase):
    def test_selected_memory_reaches_each_request_without_polluting_history(self):
        requests = []
        replies = iter([
            {"role": "assistant", "tool_calls": [{"id": "read", "type": "function", "function": {
                "name": "read_file", "arguments": '{"file_path":"source.txt"}',
            }}]},
            {"role": "assistant", "content": "样例应使用 UTF-8。"},
            {"role": "assistant", "content": "记忆已停用。"},
            {"role": "assistant", "content": "新问题的回答。"},
        ])

        def model(messages, *_, **__):
            requests.append(copy.deepcopy(messages))
            return next(replies)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "source.txt").write_text("实际样例", encoding="utf-8")

            def inputs():
                yield "/memory use 编码习惯"
                yield "读取样例并按约定回答"
                saved = Session("memory_context").load()
                self.assertEqual(saved[0]["content"], "读取样例并按约定回答")
                yield "/memory off"
                yield "停用后的问题"
                yield "/memory use 编码习惯"
                yield "/clear"
                yield "清空后的问题"
                yield "exit"

            with (
                patch("personal_agent.memory.PROJECT_ROOT", root),
                patch("personal_agent.skills.PROJECT_ROOT", root),
                patch("personal_agent.skills.USER_SKILLS_ROOT", root / "absent-user/.personal/skills"),
                patch("personal_agent.session.PROJECT_ROOT", root),
                patch("personal_agent.tools.PROJECT_ROOT", root),
                patch("personal_agent.main.get_model_message", side_effect=model),
                patch("builtins.input", side_effect=inputs()), redirect_stdout(io.StringIO()),
            ):
                save_memory("编码习惯", "项目统一使用 UTF-8。")
                history = run_repl(session_name="memory_context", with_tools=True, permission_mode="plan")
                self.assertEqual(Session("memory_context").load(), history)
                self.assertEqual(read_memory("编码习惯")["content"], "项目统一使用 UTF-8。")
            self.assertEqual(len(requests), 4)
            for messages in requests[:2]:
                question = [m for m in messages if m["role"] == "user"][-1]
                self.assertIn("项目统一使用 UTF-8。", question["content"])
                self.assertEqual(question["content"].count("<project_memory>"), 1)
                self.assertTrue(any(m["role"] == "system" and "planning" in m["content"] for m in messages))
            self.assertIn("实际样例", requests[1][-1]["content"])
            for messages in requests[2:]:
                self.assertFalse(any("<project_memory>" in str(m.get("content")) for m in messages))
            self.assertEqual(history[0]["content"], "清空后的问题")
            self.assertEqual(len(history), 2)
