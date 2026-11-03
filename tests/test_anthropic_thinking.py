"""思考内容的两个核心流程：SDK/会话往返与配置、完整性、旧格式边界。"""

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex.verify_anthropic_thinking import thinking_events, verify_offline
from personal_agent.anthropic_adapter import parse_message, prepare_messages
from personal_agent.anthropic_stream import MessageStream
from personal_agent.config import anthropic_request_options, get_model_settings
from personal_agent.context import compact_context
from personal_agent.llm import _prepare_request
from personal_agent.model_content import validate_anthropic_state
from personal_agent.session import Session, validate_agent_messages


class AnthropicThinkingTests(unittest.TestCase):
    def test_exact_sdk_echo_streaming_and_text_session_roundtrips(self):
        self.assertEqual(verify_offline()["status"], "passed")

    def test_configuration_integrity_and_old_session_compatibility(self):
        blocks = [{"type": "thinking", "thinking": "synthetic", "signature": "opaque=="},
                  {"type": "tool_use", "id": "read", "name": "read_file", "input": {"file_path": "a.txt"}},
                  {"type": "text", "text": "准备读取"}]
        reply = parse_message({"role": "assistant", "content": blocks, "stop_reason": "tool_use"},
                              tools_enabled=True, thinking={"type": "adaptive"}, model="offline")
        history = [{"role": "user", "content": "任务"}, reply, {"role": "tool", "tool_call_id": "read", "content": "工具结果"}]
        with self.assertRaisesRegex(ValueError, "不能切换"):
            prepare_messages(history, None, "", thinking={"type": "disabled"}, model="offline")
        original = deepcopy(history)
        wire, _ = _prepare_request(history, None)
        self.assertNotIn("anthropic", wire[1])
        self.assertEqual(history, original)
        bad = deepcopy(reply)
        bad["content"] = "不一致的正文"
        with self.assertRaisesRegex(ValueError, "不一致"):
            validate_anthropic_state(bad)
        bad = deepcopy(reply)
        bad["anthropic"]["content"][0]["signature"] = ""
        with self.assertRaisesRegex(ValueError, "签名"):
            validate_anthropic_state(bad)
        state = MessageStream(thinking={"type": "adaptive"}, model="offline")
        for event in thinking_events([{**blocks[0], "signature": ""}, *blocks[1:]], "tool_use"):
            state.feed(event)
        with self.assertRaisesRegex(RuntimeError, "签名"):
            state.finish(tools_enabled=True)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch("personal_agent.config.ENV_FILE", root / "missing"), patch.dict(os.environ, {"PERSONAL_AGENT_PROTOCOL": "anthropic", "ANTHROPIC_MODEL": "offline"}, clear=True):
                self.assertEqual(get_model_settings()["thinking"], {"type": "disabled"})
                os.environ["ANTHROPIC_THINKING"] = "adaptive"
                self.assertEqual(get_model_settings()["max_tokens"], 4096)
                os.environ["ANTHROPIC_THINKING"] = "enabled"
                with self.assertRaises(ValueError):
                    get_model_settings()
                os.environ["ANTHROPIC_THINKING_BUDGET"] = "1024"
                settings = get_model_settings()
                self.assertEqual(anthropic_request_options(settings, default_max_tokens=256)["max_tokens"], 4096)
                with self.assertRaisesRegex(ValueError, "小于"):
                    anthropic_request_options(settings, default_max_tokens=256, max_tokens=1024)
            with patch("personal_agent.session.PROJECT_ROOT", root):
                session = Session("old")
                old = [{"role": "user", "content": "旧提问"}, {"role": "assistant", "content": "旧回答"}]
                session.create()
                session.save(old)
                self.assertEqual(session.load(), old)
                tool_session = Session("old-tools", with_tools=True)
                tool_session.create()
                complete = [*history, {"role": "assistant", "content": "完成"}]
                tool_session.save(complete)
                self.assertEqual(tool_session.load(), validate_agent_messages(complete))
                doc = json.loads(tool_session.path.read_text(encoding="utf-8"))
                self.assertEqual(doc["version"], 4)
                doc["messages"][1]["anthropic"]["content"][1]["name"] = "write_file"
                tool_session.path.write_text(json.dumps(doc), encoding="utf-8")
                before = tool_session.path.read_bytes()
                with self.assertRaisesRegex(ValueError, "不一致"):
                    tool_session.load()
                self.assertEqual(tool_session.path.read_bytes(), before)
            recent = parse_message({"role": "assistant", "stop_reason": "end_turn", "content":
                                    [blocks[0], {"type": "text", "text": "保留最近"}]},
                                   tools_enabled=False, thinking={"type": "adaptive"}, model="offline")
            long_history = deepcopy(complete)
            long_history[0]["content"] = "旧材料" * 2000
            long_history.extend([{"role": "user", "content": "最近问题"}, recent])
            def summarize(request):
                self.assertNotIn('"anthropic"', request[-1]["content"])
                return {"role": "assistant", "content": json.dumps({"goal": "接续", "constraints": [], "completed": [], "pending": [], "next_steps": [], "references": []})}
            with patch("personal_agent.tool_output.PROJECT_ROOT", root):
                candidate, archive = compact_context(long_history, summarize)
            self.assertEqual(candidate[-1], recent)
            saved = json.loads((root / archive).read_text(encoding="utf-8"))
            self.assertEqual(saved[1]["anthropic"], reply["anthropic"])
