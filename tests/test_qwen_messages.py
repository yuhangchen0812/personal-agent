"""与一键验收共用实际 SDK/CLI 网关主流程。"""

import unittest

from tests.support.verify_qwen_messages import verify_workflow


class QwenMessagesTests(unittest.TestCase):
    def test_explicit_profile_tools_streaming_and_session_roundtrip(self):
        self.assertEqual(verify_workflow()["status"], "passed")
