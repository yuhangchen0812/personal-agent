"""用量的定向检查：独立累计与空 choices 的流式统计片段。"""

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from openai.types.chat import ChatCompletion, ChatCompletionChunk

from personal_agent.main import run_repl
from personal_agent.session import Session
from personal_agent.usage import UsageStats
from test_stream import chunk, response


def completion(message, usage=None, reason="stop"):
    return ChatCompletion.model_validate({
        "id": "usage-check", "object": "chat.completion", "created": 0, "model": "offline",
        "choices": [{"index": 0, "finish_reason": reason, "message": message}], "usage": usage,
    })


class UsageTests(unittest.TestCase):
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

    def test_usage_survives_history_rollback_and_missing_data_is_explicit(self):
        self.client.chat.completions.create.side_effect = [
            completion({"role": "assistant", "tool_calls": [{"id": "read_usage", "type": "function", "function": {"name": "read_file", "arguments": '{"file_path":"source.txt"}'}}]}, {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}, "tool_calls"),
            completion({"role": "assistant", "content": "截断"}, {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}, "length"),
            completion({"role": "assistant", "content": "下一轮成功"}),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "source.txt").write_text("固定文件", encoding="utf-8")
            output = io.StringIO()
            with (
                patch("personal_agent.session.PROJECT_ROOT", root), patch("personal_agent.tools.PROJECT_ROOT", root),
                patch("builtins.input", side_effect=["读取并回答", "/usage", "继续", "/usage", "exit"]),
                redirect_stdout(output), redirect_stderr(io.StringIO()),
            ):
                history = run_repl(session_name="usage_tools", with_tools=True)
                self.assertEqual(Session("usage_tools").load(), history)
            self.assertEqual(history, [{"role": "user", "content": "继续"}, {"role": "assistant", "content": "下一轮成功"}])
            self.assertIn("逻辑调用 3 次，完整用量 2 次，缺失 1 次", output.getvalue())
            self.assertIn("输入 30，输出 7，总计 37", output.getvalue())
            self.assertNotIn("usage", history[-1])
        stats = UsageStats()
        stats.record({"prompt_tokens": 8})
        self.assertIn("暂无完整服务端统计", stats.format_summary())

    def test_stream_usage_chunk_and_statistics_reset_after_resume(self):
        usage_chunk = ChatCompletionChunk.model_validate({
            "id": "last-usage", "object": "chat.completion.chunk", "created": 0, "model": "offline", "choices": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
        })
        self.client.chat.completions.create.return_value = response(iter([chunk("你好"), chunk(reason="stop"), usage_chunk]))
        with tempfile.TemporaryDirectory() as temporary, patch("personal_agent.session.PROJECT_ROOT", Path(temporary)):
            output = io.StringIO()
            with patch("builtins.input", side_effect=["你好", "/usage", "exit"]), redirect_stdout(output):
                history = run_repl(session_name="usage_stream", stream=True)
            self.assertIn("逻辑调用 1 次，完整用量 1 次，缺失 0 次", output.getvalue())
            self.assertIn("输入 10，输出 3，总计 13", output.getvalue())
            self.assertEqual(self.client.chat.completions.create.call_args.kwargs["stream_options"], {"include_usage": True})
            output = io.StringIO()
            with patch("builtins.input", side_effect=["/usage", "exit"]), redirect_stdout(output):
                restored = run_repl(session_name="usage_stream", resume=True)
            self.assertEqual(restored, history)
            self.assertIn("逻辑调用 0 次", output.getvalue())
            self.client.chat.completions.create.assert_called_once()
