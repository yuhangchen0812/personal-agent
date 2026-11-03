"""Anthropic 流的两个核心场景，模型事件固定，SDK/SSE 和本地工具真实。"""

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import httpx2
from anthropic import Anthropic

from codex.verify_anthropic_stream import EventBody, stream_events, verify_offline
from personal_agent.main import run_repl
from personal_agent.session import Session


class AnthropicStreamTests(unittest.TestCase):
    def test_sdk_stream_tools_approval_usage_retry_and_restore(self):
        self.assertEqual(verify_offline()["status"], "passed")

    def test_incomplete_invalid_disconnected_and_cancelled_streams_execute_nothing(self):
        tool = {"type": "tool_use", "id": "valid", "name": "write_file", "input": {"file_path": "playground/never.txt", "content": "不能提前写"}}
        bad_json = stream_events([tool, {**tool, "id": "bad"}], "tool_use")
        for event in bad_json:
            if event["type"] == "content_block_delta" and event["index"] == 1:
                event["delta"]["partial_json"] = "{"
        interrupted = stream_events([tool], "tool_use", stop=False)[:-2]
        cases = [
            ("missing_stop", stream_events([tool], "tool_use", stop=False), "message_stop"),
            ("bad_json_batch", bad_json, "JSON"),
            ("truncated", stream_events([tool], "max_tokens"), "输出上限"),
            ("thinking", stream_events([{"type": "thinking", "thinking": "", "signature": ""}]), "思考块"),
            ("read_error", [*interrupted, httpx2.ReadError("private transport error")], "APIConnectionError"),
            ("cancelled", [*interrupted, KeyboardInterrupt()], "已中断"),
        ]
        for name, events, expected in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                bodies, requests = [], []
                config = {"protocol": "anthropic", "api_key": "offline", "model": "offline", "base_url": "https://example.invalid"}
                def handle(request):
                    requests.append(request)
                    content = EventBody(events if len(requests) == 1 else stream_events([{"type": "text", "text": "继续回答"}]))
                    bodies.append(content)
                    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=content)
                def client(**options):
                    return Anthropic(**options, http_client=httpx2.Client(transport=httpx2.MockTransport(handle)))
                output, errors = io.StringIO(), io.StringIO()
                with (
                    patch("personal_agent.main.get_model_settings", return_value=config),
                    patch("personal_agent.llm.load_model_config", return_value=config),
                    patch("personal_agent.anthropic_stream.Anthropic", side_effect=client),
                    patch("personal_agent.session.PROJECT_ROOT", root),
                    patch("personal_agent.main.execute_tool") as dispatch,
                    patch("personal_agent.main.discover_agents", return_value=([], [])),
                    patch("personal_agent.main.discover_skills", return_value=([], [])),
                    patch("builtins.input", side_effect=["失败的任务", "/usage", "下一问", "exit"]),
                    redirect_stdout(output), redirect_stderr(errors),
                ):
                    history = run_repl(session_name="failure", with_tools=True, stream=True)
                    self.assertEqual(Session("failure").load(), history)
                dispatch.assert_not_called()
                self.assertEqual(len(requests), 2)  # 断流、坏内容或取消均没有重跑模型请求。
                self.assertTrue(all(body.closed for body in bodies))
                self.assertEqual(history, [{"role": "user", "content": "下一问"}, {"role": "assistant", "content": "继续回答"}])
                self.assertIn(expected, output.getvalue() + errors.getvalue())
                self.assertNotIn("private transport error", errors.getvalue())
                self.assertFalse((root / "playground").exists())
                if name in {"missing_stop", "bad_json_batch", "truncated"}:
                    self.assertIn("输入 30，输出 7，总计 37", output.getvalue())
                else:
                    self.assertIn("缺失 1 次", output.getvalue())
