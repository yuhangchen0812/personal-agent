"""用真实 SDK 和离线 HTTP 传输检查有限重试，不访问外部服务。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx2
from openai import APIConnectionError, AuthenticationError, OpenAI

from personal_agent.agent import run_agent_loop
from personal_agent.llm import get_model_message, stream_model_reply
from personal_agent.tool_registry import execute_tool


def completion(message):
    return {"id": "offline-retry", "object": "chat.completion", "created": 0, "model": "offline", "choices": [{"index": 0, "finish_reason": "tool_calls" if message.get("tool_calls") else "stop", "message": message}]}


class RequestRetryTests(unittest.TestCase):
    def test_retry_model_request_without_replaying_tool_and_no_retry_for_401(self):
        requests = []
        def handle(request):
            requests.append(json.loads(request.content))
            if len(requests) == 2:
                return httpx2.Response(503, headers={"retry-after": "0.001"}, json={"error": {"message": "temporary", "type": "server_error"}})
            message = ({"role": "assistant", "tool_calls": [{"id": "read_once", "type": "function", "function": {"name": "read_file", "arguments": '{"file_path":"source.txt"}'}}]}
                       if len(requests) == 1 else {"role": "assistant", "content": "已读取。"})
            return httpx2.Response(200, json=completion(message))

        def client(**options):
            return OpenAI(**options, http_client=httpx2.Client(transport=httpx2.MockTransport(handle)))

        config = {"api_key": "offline", "base_url": "https://example.invalid/v1", "model": "offline"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "source.txt").write_text("固定内容", encoding="utf-8")
            with (
                patch("personal_agent.tools.PROJECT_ROOT", root),
                patch("personal_agent.llm.load_model_config", return_value=config),
                patch("personal_agent.llm.OpenAI", side_effect=client),
                patch("personal_agent.agent.execute_tool", wraps=execute_tool) as tool,
            ):
                messages = [{"role": "user", "content": "读取文件"}]
                self.assertEqual(run_agent_loop(messages, get_model_message, max_turns=2), "已读取。")
            self.assertEqual(len(requests), 3)  # 工具已执行，第二次逻辑模型调用发生一次 HTTP 重试。
            self.assertEqual(requests[1], requests[2])
            tool.assert_called_once()

        failed_requests = []
        def denied(request):
            failed_requests.append(request)
            return httpx2.Response(401, json={"error": {"message": "invalid", "type": "authentication_error"}})
        def denied_client(**options):
            return OpenAI(**options, http_client=httpx2.Client(transport=httpx2.MockTransport(denied)))
        with patch("personal_agent.llm.load_model_config", return_value=config), patch("personal_agent.llm.OpenAI", side_effect=denied_client):
            with self.assertRaises(AuthenticationError):
                get_model_message([{"role": "user", "content": "你好"}])
        self.assertEqual(len(failed_requests), 1)

        stream_requests = []
        class BrokenBody(httpx2.SyncByteStream):
            def __iter__(self):
                payload = {"id": "partial", "object": "chat.completion.chunk", "created": 0, "model": "offline", "choices": [{"index": 0, "delta": {"content": "半截"}}]}
                yield ("data: " + json.dumps(payload) + "\n\n").encode()
                raise httpx2.ReadError("模拟中途断流")
        def broken(request):
            stream_requests.append(request)
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=BrokenBody())
        def broken_client(**options):
            return OpenAI(**options, http_client=httpx2.Client(transport=httpx2.MockTransport(broken)))
        fragments = []
        with patch("personal_agent.llm.load_model_config", return_value=config), patch("personal_agent.llm.OpenAI", side_effect=broken_client):
            with self.assertRaises(APIConnectionError):
                stream_model_reply([{"role": "user", "content": "你好"}], fragments.append)
        self.assertEqual(len(stream_requests), 1)
        self.assertEqual(fragments, ["半截"])
