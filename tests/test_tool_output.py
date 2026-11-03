"""一条实际工具链检查长结果落盘、字符读取和会话恢复。"""

import copy
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from personal_agent.main import run_repl
from personal_agent.session import Session
from personal_agent.tool_output import MAX_TOOL_RESULT_CHARS, prepare_tool_result
from personal_agent.tool_registry import execute_tool
from personal_agent.tools import ERROR_PREFIX, read_file


class ToolOutputSmokeTest(unittest.TestCase):
    def test_large_result_can_be_read_back_without_losing_call_pairs(self):
        marker = "只在中间出现的关键标记"
        source = "前段|" + "甲" * MAX_TOOL_RESULT_CHARS + marker + "乙" * MAX_TOOL_RESULT_CHARS + "|末尾"
        full_result = "   1 | " + source
        requests = []
        result_path = None

        def tool_call(call_id, arguments):
            return {"role": "assistant", "tool_calls": [{
                "id": call_id, "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps(arguments)},
            }]}

        def model(messages, definitions, **_):
            nonlocal result_path
            requests.append(copy.deepcopy(messages))
            if len(requests) == 1:
                return tool_call("long_read", {"file_path": "long.txt"})
            if len(requests) == 2:
                preview = messages[-1]["content"]
                self.assertEqual(messages[-1]["tool_call_id"], "long_read")
                self.assertLess(len(preview), MAX_TOOL_RESULT_CHARS)
                self.assertIn("工具结果已截断", preview)
                self.assertIn("前段|", preview)
                self.assertIn("|末尾", preview)
                self.assertNotIn(marker, preview)
                result_path = re.search(r"完整结果：([^\n]+)", preview).group(1)
                return tool_call("middle_read", {
                    "file_path": result_path,
                    "char_offset": full_result.index(marker) + 1, "char_limit": 64,
                })
            self.assertEqual(messages[-1]["tool_call_id"], "middle_read")
            self.assertIn(marker, messages[-1]["content"])
            self.assertNotIn("工具结果已截断", messages[-1]["content"])
            return {"role": "assistant", "content": "已从完整结果中读到关键标记。"}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "long.txt").write_text(source, encoding="utf-8")
            with (
                patch("personal_agent.tool_output.PROJECT_ROOT", root),
                patch("personal_agent.tools.PROJECT_ROOT", root),
                patch("personal_agent.session.PROJECT_ROOT", root),
                patch("personal_agent.main.get_model_message", side_effect=model),
                patch("personal_agent.main.execute_tool", wraps=execute_tool) as dispatch,
                patch("builtins.input", side_effect=["读取长结果中的关键标记", "exit"]),
                redirect_stdout(io.StringIO()),
            ):
                history = run_repl(session_name="large_result", with_tools=True)
                self.assertEqual(dispatch.call_count, 2)
                self.assertEqual(Session("large_result").load(), history)
                files = list((root / ".personal/tool-results").glob("result-*.txt"))
                self.assertEqual(len(files), 1)
                self.assertEqual(files[0].read_text(encoding="utf-8"), full_result)
                self.assertIn(marker, read_file(result_path, char_offset=full_result.index(marker) + 1, char_limit=64))
                self.assertTrue(read_file(".personal/sessions/large_result.json").startswith(ERROR_PREFIX))
                with patch("personal_agent.tool_output._save_full_result", side_effect=OSError):
                    fallback = prepare_tool_result(full_result)
                    self.assertIn("保存失败", fallback)
                    self.assertTrue(fallback.endswith(full_result))
        self.assertEqual(len(requests), 3)
