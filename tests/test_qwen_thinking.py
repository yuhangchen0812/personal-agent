import unittest

from codex.verify_qwen_thinking import verify_workflow


class QwenThinkingTests(unittest.TestCase):
    def test_reasoning_tool_flow_stream_and_session(self):
        self.assertEqual(verify_workflow()["status"],"passed")
