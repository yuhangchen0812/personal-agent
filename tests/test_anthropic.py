"""第二协议的两条核心流程：实际 SDK 往返，以及关键错误前置拦截。"""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx2
from anthropic import Anthropic, AuthenticationError
from anthropic.types import Message

from tests.support.verify_dual_protocol import response, verify_offline
from personal_agent.anthropic_adapter import normalize_usage, parse_message, prepare_messages
from personal_agent.config import get_model_settings, load_model_config
from personal_agent.llm import get_model_message, stream_model_message
from personal_agent.main import run_repl
from personal_agent.usage import TokenPrices, UsageStats


class AnthropicTests(unittest.TestCase):
    def test_sdk_cli_tools_retry_usage_and_session_roundtrip(self):
        report = verify_offline()
        self.assertEqual(report["status"], "passed")
        self.assertTrue(report["write_denied"])
        self.assertFalse(report["resume_replayed_tools"])

    def test_configuration_incomplete_response_budget_and_api_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("PERSONAL_AGENT_PROTOCOL=anthropic\nANTHROPIC_MODEL=file-model\nANTHROPIC_API_KEY=file-key\n", encoding="utf-8")
            with patch("personal_agent.config.ENV_FILE", env_file), patch.dict(os.environ, {"ANTHROPIC_MODEL": "environment-model"}, clear=True):
                config = load_model_config()
                self.assertEqual(config["model"], "environment-model")
                self.assertEqual(config["api_key"], "file-key")
                self.assertNotIn("api_key", get_model_settings())
            with patch("personal_agent.config.ENV_FILE", Path(temporary) / "missing"), patch.dict(os.environ, {"PERSONAL_AGENT_PROTOCOL": "anthropic"}, clear=True):
                with self.assertRaisesRegex(ValueError, "ANTHROPIC_MODEL"):
                    get_model_settings()
                os.environ["ANTHROPIC_MODEL"] = "configured"
                with self.assertRaisesRegex(ValueError, "ANTHROPIC_API_KEY"):
                    load_model_config()
                os.environ["PERSONAL_AGENT_PROTOCOL"] = "unknown"
                with self.assertRaisesRegex(ValueError, "openai 或 anthropic"):
                    get_model_settings()

        block = {"type": "tool_use", "id": "once", "name": "read_file", "input": {"file_path": "demo.txt"}}
        for document in (response([block], "max_tokens"), response([block, block], "tool_use"),
                         response([block]), response([{"type": "thinking", "thinking": "unsupported"}])):
            with self.assertRaises(RuntimeError):
                parse_message(document, tools_enabled=True)
        with self.assertRaises(RuntimeError):
            parse_message(response([block], "tool_use"), tools_enabled=False)
        with self.assertRaisesRegex(ValueError, "前置"):
            prepare_messages([{"role": "user", "content": "用户"}, {"role": "system", "content": "不能提升到顶层"}], None, "")
        stats = UsageStats()
        ordinary = response([{"type": "text", "text": "普通回复"}])
        ordinary["usage"] = {"input_tokens": 20, "output_tokens": 5}
        self.assertEqual(normalize_usage(Message.model_validate(ordinary).model_dump()["usage"])["total_tokens"], 25)
        stats.record(normalize_usage({"input_tokens": 1}))
        with self.assertRaisesRegex(RuntimeError, "缺少完整用量"):
            stats.check_budget(TokenPrices(Decimal(1), Decimal(1)), Decimal(1))

        config = {"protocol": "anthropic", "model": "offline", "base_url": "https://example.invalid", "api_key": "offline"}
        requests = []
        def handle(request):
            requests.append(request)
            return httpx2.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "secret-error-body"}})
        def client(**options):
            return Anthropic(**options, http_client=httpx2.Client(transport=httpx2.MockTransport(handle)))
        with patch("personal_agent.llm.load_model_config", return_value=config), \
             patch("personal_agent.anthropic_adapter.Anthropic", side_effect=client), \
             patch("personal_agent.anthropic_stream.Anthropic", side_effect=client):
            with self.assertRaises(AuthenticationError):
                get_model_message([{"role": "user", "content": "你好"}])
            self.assertEqual(len(requests), 1)  # 401 不重试。
            with self.assertRaises(AuthenticationError):
                stream_model_message([{"role": "user", "content": "你好"}])
            self.assertEqual(len(requests), 2)
            output, errors = io.StringIO(), io.StringIO()
            with patch("personal_agent.main.get_model_settings", return_value=config), \
                 patch("builtins.input", side_effect=["你好", "exit"]), redirect_stdout(output), redirect_stderr(errors):
                self.assertEqual(run_repl(), [])
            self.assertIn("AuthenticationError", errors.getvalue())
            self.assertNotIn("secret-error-body", errors.getvalue())
            self.assertEqual(len(requests), 3)
