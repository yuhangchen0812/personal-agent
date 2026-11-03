"""文本流的定向检查：即时显示、完整保存，以及失败后不保留半轮。"""

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


def chunk(text=None, reason=None):
    return ChatCompletionChunk.model_validate({
        "id": "offline-stream", "object": "chat.completion.chunk", "created": 0, "model": "offline",
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": reason}],
    })


def response(chunks):
    stream = MagicMock()
    stream.__enter__.return_value = chunks
    stream.__exit__.return_value = False
    return stream


class TextStreamTests(unittest.TestCase):
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

    def test_fragments_display_before_completion_and_save_once(self):
        with tempfile.TemporaryDirectory() as temporary, patch("personal_agent.session.PROJECT_ROOT", Path(temporary)):
            output = io.StringIO()
            def chunks():
                yield chunk()  # 只带角色/空内容的起始块不输出。
                yield chunk("你")
                self.assertIn("Personal Agent > 你", output.getvalue())
                self.assertEqual(Session("stream_demo").load(), [])
                yield chunk("好！")
                yield chunk(reason="stop")
            stream = response(chunks())
            self.client.chat.completions.create.return_value = stream
            with patch("builtins.input", side_effect=["你好", "exit"]), redirect_stdout(output):
                history = run_repl(session_name="stream_demo", stream=True)
            self.assertEqual(history, [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好！"}])
            self.assertEqual(Session("stream_demo").load(), history)
            self.assertEqual(output.getvalue().count("Personal Agent > 你好！"), 1)
            options = self.client.chat.completions.create.call_args.kwargs
            self.assertTrue(options["stream"])
            self.assertNotIn("tools", options)
            stream.__exit__.assert_called_once()

    def test_disconnect_limit_and_interrupt_roll_back_before_next_input(self):
        def interrupted():
            yield chunk("中断前的片段")
            raise KeyboardInterrupt()
        streams = [
            response(iter([chunk("断流的片段")])),
            response(iter([chunk("超限的片段"), chunk(reason="length")])),
            response(interrupted()),
            response(iter([chunk("完整回答"), chunk(reason="stop")])),
        ]
        remaining = iter(streams)
        requests = []
        def create(**options):
            requests.append(copy.deepcopy(options["messages"]))
            return next(remaining)
        self.client.chat.completions.create.side_effect = create
        with tempfile.TemporaryDirectory() as temporary, patch("personal_agent.session.PROJECT_ROOT", Path(temporary)):
            output, errors = io.StringIO(), io.StringIO()
            with (
                patch("builtins.input", side_effect=["断流", "超限", "中断", "成功的问题", "exit"]),
                redirect_stdout(output), redirect_stderr(errors),
            ):
                history = run_repl(session_name="retry_demo", stream=True)
            self.assertEqual(requests[-1], [{"role": "user", "content": "成功的问题"}])
            self.assertEqual(Session("retry_demo").load(), history)
            self.assertEqual(history[-1]["content"], "完整回答")
            self.assertEqual(len(history), 2)
            self.assertIn("结束标记", errors.getvalue())
            self.assertIn("输出上限", errors.getvalue())
            for stream in streams:
                stream.__exit__.assert_called_once()
