"""一个固定场景检查工具会话保存/恢复及关键权限边界；不请求模型。"""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from personal_agent.main import run_repl
from personal_agent.session import Session
from personal_agent.tool_registry import execute_tool


def call(call_id, name, arguments):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


class ToolSessionTests(unittest.TestCase):
    def test_save_restore_without_replay_and_recheck_new_operations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "source.txt").write_text("实际文件内容\n", encoding="utf-8")
            requests = []
            replies = iter([
                {"role": "assistant", "content": None, "tool_calls": [
                    call("read_1", "read_file", {"file_path": "source.txt"}),
                    call("write_1", "write_file", {"file_path": "playground/approved.txt", "content": "已批准"}),
                ]},
                {"role": "assistant", "content": "已读取并写入。"},
                {"role": "assistant", "tool_calls": [
                    call("read_2", "read_file", {"file_path": "source.txt"}),
                    call("write_2", "write_file", {"file_path": "playground/refused.txt", "content": "不应写入"}),
                ]},
                {"role": "assistant", "content": "新操作未执行。"},
            ])

            def model(messages, definitions, **_):
                self.assertIn("enter_plan_mode", [tool["name"] for tool in definitions])
                requests.append(copy.deepcopy(messages))
                return next(replies)

            with (
                patch("personal_agent.session.PROJECT_ROOT", root),
                patch("personal_agent.tools.PROJECT_ROOT", root),
                patch("personal_agent.main.discover_skills", return_value=([], [])),
                patch("personal_agent.main.get_model_message", side_effect=model),
                patch("personal_agent.main.execute_tool", wraps=execute_tool) as dispatch,
                redirect_stdout(io.StringIO()),
            ):
                with patch("builtins.input", side_effect=["读取并写入", "y", "exit"]):
                    first = run_repl(session_name="tools_demo", with_tools=True)
                session = Session("tools_demo")
                self.assertEqual(session.load(), first)
                self.assertTrue(session.with_tools)
                self.assertEqual(dispatch.call_count, 2)

                with patch("builtins.input", side_effect=["继续，但不要批准新写入", "n", "exit"]):
                    final = run_repl(session_name="tools_demo", resume=True)
                self.assertEqual(requests[2][1:], first + [{"role": "user", "content": "继续，但不要批准新写入"}])
                self.assertEqual(final[:len(first)], first)
                self.assertEqual(session.load(), final)
                self.assertEqual(dispatch.call_count, 4)  # 只执行本次新请求，旧调用未重放。
                self.assertEqual((root / "playground/approved.txt").read_text(encoding="utf-8"), "已批准")
                self.assertFalse((root / "playground/refused.txt").exists())
                self.assertIn("未获批准", final[-2]["content"])
                self.assertIn("实际文件内容", final[-3]["content"])

                document = json.loads(session.path.read_text(encoding="utf-8"))
                del document["messages"][2]  # 删掉 read_1 的结果，制造缺失配对。
                session.path.write_text(json.dumps(document), encoding="utf-8")
                before = len(requests)
                with self.assertRaises(ValueError):
                    run_repl(session_name="tools_demo", resume=True)
                self.assertEqual(len(requests), before)
