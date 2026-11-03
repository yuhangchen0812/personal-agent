"""一条两轮工具会话检查旧结果收缩、原文件复用和按引用回读。"""

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
from personal_agent.tool_output import MAX_TOOL_RESULT_CHARS, OLD_TOOL_RESULT_CHARS, shrink_old_tool_results
from personal_agent.tool_registry import execute_tool


class OldToolOutputSmokeTest(unittest.TestCase):
    def test_only_previous_round_results_shrink_and_remain_readable(self):
        marker = "旧结果中间的关键标记"
        medium = "甲" * 1500 + marker + "乙" * 1500
        large = "长结果" * MAX_TOOL_RESULT_CHARS
        requests = []
        first_history = None
        medium_reference = None

        def call(call_id, file_path, **arguments):
            return {"id": call_id, "type": "function", "function": {
                "name": "read_file", "arguments": json.dumps({"file_path": file_path, **arguments}),
            }}

        def model(messages, *_, **__):
            nonlocal first_history, medium_reference
            requests.append(copy.deepcopy(messages))
            if len(requests) == 1:
                return {"role": "assistant", "tool_calls": [
                    call("medium", "medium.txt"), call("large", "large.txt"),
                ]}
            if len(requests) == 2:
                results = [message for message in messages if message["role"] == "tool"]
                self.assertIn(marker, results[0]["content"])  # 当前任务未收缩。
                self.assertNotIn("旧工具结果已收缩", results[1]["content"])
                first_history = copy.deepcopy(messages)
                return {"role": "assistant", "content": "第一轮完成。"}
            if len(requests) == 3:
                results = [message for message in messages if message["role"] == "tool"]
                self.assertEqual([message["tool_call_id"] for message in results], ["medium", "large"])
                self.assertTrue(all(len(message["content"]) < OLD_TOOL_RESULT_CHARS for message in results))
                original_large_path = re.search(r"完整结果：([^\n]+)", first_history[-1]["content"]).group(1)
                self.assertIn(f"完整结果：{original_large_path}\n", results[1]["content"])
                medium_reference = re.search(r"完整结果：([^\n]+)", results[0]["content"]).group(1)
                return {"role": "assistant", "tool_calls": [call(
                    "read_back", medium_reference, char_offset=("   1 | " + medium).index(marker) + 1, char_limit=64,
                )]}
            self.assertIn(marker, messages[-1]["content"])
            return {"role": "assistant", "content": "已回读旧结果。"}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "medium.txt").write_text(medium, encoding="utf-8")
            (root / "large.txt").write_text(large, encoding="utf-8")
            with (
                patch("personal_agent.tool_output.PROJECT_ROOT", root),
                patch("personal_agent.tools.PROJECT_ROOT", root),
                patch("personal_agent.session.PROJECT_ROOT", root),
                patch("personal_agent.main.get_model_message", side_effect=model),
                patch("personal_agent.main.execute_tool", wraps=execute_tool) as dispatch,
                patch("builtins.input", side_effect=["先读取两份文件", "回读旧结果中的标记", "exit"]),
                redirect_stdout(io.StringIO()),
            ):
                history = run_repl(session_name="old_results", with_tools=True)
                self.assertEqual(dispatch.call_count, 3)
                self.assertEqual(Session("old_results").load(), history)
                self.assertEqual(len(list((root / ".personal/tool-results").glob("result-*.txt"))), 2)
                self.assertEqual((root / medium_reference).read_text(encoding="utf-8"), "   1 | " + medium)
                unchanged = copy.deepcopy(history)
                self.assertEqual(shrink_old_tool_results(history), 0)
                self.assertEqual(history, unchanged)
                failed_history = copy.deepcopy(history)
                failed_history[2]["content"] = medium
                with patch("personal_agent.tool_output._save_full_result", side_effect=OSError):
                    self.assertEqual(shrink_old_tool_results(failed_history), 0)
                self.assertEqual(failed_history[2]["content"], medium)
        self.assertEqual(len(requests), 4)
        self.assertEqual([m["content"] for m in history if m["role"] == "user"], ["先读取两份文件", "回读旧结果中的标记"])
