"""模型适配的两个核心测试，不请求真实接口。"""

import unittest
from unittest.mock import Mock, patch

from openai.types.chat import ChatCompletion

from personal_agent.llm import get_model_message, get_model_reply
from personal_agent.tool_registry import get_tool_definitions


def completion(message, finish_reason="stop"):
    return ChatCompletion.model_validate({
        "id": "offline-test", "object": "chat.completion", "created": 0, "model": "test-model",
        "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
    })


class ModelAdapterTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        sdk = Mock()
        sdk.return_value.__enter__ = Mock(return_value=self.client)
        sdk.return_value.__exit__ = Mock(return_value=False)
        sdk_patch = patch("personal_agent.llm.OpenAI", sdk)
        sdk_patch.start()
        self.addCleanup(sdk_patch.stop)
        config_patch = patch("personal_agent.llm.load_model_config", return_value={"api_key": "offline", "base_url": "https://example.invalid/v1", "model": "test-model"})
        config_patch.start()
        self.addCleanup(config_patch.stop)

    def test_tools_schema_and_message_are_preserved(self):
        message = {"role": "assistant", "content": None, "tool_calls": [{"id": "call_test", "type": "function", "function": {"name": "read_file", "arguments": '{"file_path":"demo.txt"}'}}]}
        raw = {**message, "tool_calls": [{**message["tool_calls"][0], "index": 0,
               "function": {**message["tool_calls"][0]["function"], "provider_extra": "response-only"}}]}
        self.client.chat.completions.create.return_value = completion(raw, "tool_calls")
        history = [{"role": "user", "content": "读取文件"}]
        definitions = get_tool_definitions()
        self.assertEqual(get_model_message(history, definitions), message)
        sent = self.client.chat.completions.create.call_args.kwargs
        self.assertEqual(sent["tools"][0]["function"]["parameters"], definitions[0]["input_schema"])
        self.assertEqual(sent["tool_choice"], "auto")
        self.assertEqual(sent["messages"][0]["role"], "system")
        self.assertEqual(history, [{"role": "user", "content": "读取文件"}])
        self.assertEqual(sent["max_tokens"], 1024)

    def test_text_wrapper_and_output_limit(self):
        self.client.chat.completions.create.return_value = completion({"role": "assistant", "content": " 收到 "})
        self.assertEqual(get_model_reply([{"role": "user", "content": "你好"}]), "收到")
        sent = self.client.chat.completions.create.call_args.kwargs
        self.assertNotIn("tools", sent)
        self.assertEqual(sent["max_tokens"], 256)
        self.client.chat.completions.create.return_value = completion({"role": "assistant", "content": "截断"}, "length")
        with self.assertRaisesRegex(RuntimeError, "输出上限"):
            get_model_message([{"role": "user", "content": "你好"}], get_tool_definitions())
