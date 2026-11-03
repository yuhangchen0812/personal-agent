"""工具流的两个定向场景；模型片段固定，文件工具实际执行。"""

import copy
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from openai.types.chat import ChatCompletionChunk

from personal_agent.main import run_repl
from personal_agent.session import Session
from personal_agent.tool_registry import execute_tool
from test_stream import chunk, response


def tool_delta(index, *, call_id=None, name=None, arguments=None, reason=None):
    delta = {"index": index, "id": call_id, "function": {"name": name, "arguments": arguments}}
    return ChatCompletionChunk.model_validate({
        "id": "offline-tools", "object": "chat.completion.chunk", "created": 0, "model": "offline",
        "choices": [{"index": 0, "delta": {"tool_calls": [delta]}, "finish_reason": reason}],
    })


class ToolStreamTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        sdk = MagicMock()
        sdk.return_value.__enter__.return_value = self.client
        for mock in [
            patch("personal_agent.llm.OpenAI", sdk),
            patch("personal_agent.llm.load_model_config", return_value={"api_key": "offline", "base_url": "https://example.invalid/v1", "model": "offline"}),
        ]:
            mock.start()
            self.addCleanup(mock.stop)

    def test_interleaved_calls_execute_once_after_finish_and_save_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "source.txt").write_text("实际读取的中文\n", encoding="utf-8")
            requests = []
            def chunks():
                yield tool_delta(0, call_id="write_", name="write_", arguments='{"file_path":')
                dispatch.assert_not_called()
                yield tool_delta(1, call_id="read_1", name="read_file", arguments='{"file_path":"source.txt"}')
                yield tool_delta(0, call_id="1", name="file", arguments='"playground/stream.txt","content":"实际写入"}')
                dispatch.assert_not_called()  # 参数已拼齐，结束标记前仍不能执行。
                yield chunk(reason="tool_calls")
            streams = iter([response(chunks()), response(iter([chunk("已完成"), chunk("。"), chunk(reason="stop")]))])
            def create(**options):
                self.assertTrue(options["stream"])
                self.assertEqual(options["tool_choice"], "auto")
                requests.append(copy.deepcopy(options["messages"]))
                return next(streams)
            self.client.chat.completions.create.side_effect = create
            output = io.StringIO()
            with (
                patch("personal_agent.session.PROJECT_ROOT", root), patch("personal_agent.tools.PROJECT_ROOT", root),
                patch("personal_agent.main.execute_tool", wraps=execute_tool) as dispatch,
                patch("builtins.input", side_effect=["读写练习文件", "y", "exit"]), redirect_stdout(output),
            ):
                history = run_repl(session_name="stream_tools", with_tools=True, stream=True)
                self.assertEqual(Session("stream_tools").load(), history)
            self.assertEqual(dispatch.call_count, 2)
            results = [message for message in requests[1] if message["role"] == "tool"]
            self.assertEqual([message["tool_call_id"] for message in results], ["write_1", "read_1"])
            self.assertIn("实际读取的中文", results[1]["content"])
            self.assertEqual((root / "playground/stream.txt").read_text(encoding="utf-8"), "实际写入")
            self.assertEqual(output.getvalue().count("Personal Agent > 已完成。"), 1)

    def test_unfinished_or_invalid_arguments_do_not_execute(self):
        streams = [
            response(iter([tool_delta(0, call_id="pending", name="write_file", arguments='{"file_path":"playground/no.txt","content":"不能写"}')])),
            response(iter([tool_delta(0, call_id="bad", name="write_file", arguments='{"file_path":', reason="tool_calls")])),
            response(iter([chunk("继续聊天"), chunk(reason="stop")])),
        ]
        self.client.chat.completions.create.side_effect = streams
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            errors = io.StringIO()
            with (
                patch("personal_agent.session.PROJECT_ROOT", root), patch("personal_agent.tools.PROJECT_ROOT", root),
                patch("personal_agent.main.execute_tool") as dispatch,
                patch("builtins.input", side_effect=["断流", "坏参数", "下一问", "exit"]),
                redirect_stdout(io.StringIO()), redirect_stderr(errors),
            ):
                history = run_repl(session_name="broken_tools", with_tools=True, stream=True)
            dispatch.assert_not_called()
            self.assertEqual(history, [{"role": "user", "content": "下一问"}, {"role": "assistant", "content": "继续聊天"}])
            self.assertFalse((root / "playground").exists())
            self.assertIn("结束标记", errors.getvalue())
            self.assertIn("JSON", errors.getvalue())
            for stream in streams:
                stream.__exit__.assert_called_once()
