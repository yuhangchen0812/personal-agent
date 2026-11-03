"""Agent Loop 的三个核心流程测试，不请求真实模型。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from personal_agent.agent import run_agent_loop
from personal_agent.tool_registry import TOOL_ERROR_PREFIX, TOOL_REGISTRY, execute_tool


def tool_call(call_id, name, arguments):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def scripted_model(responses, requests):
    responses = iter(responses)

    def call_model(messages, definitions):
        requests.append(copy.deepcopy(messages))
        return next(responses)

    return call_model


class AgentLoopTests(unittest.TestCase):
    def test_tool_results_feed_back_before_final_answer(self):
        requests = []
        messages = [{"role": "user", "content": "找到并读取 greet"}]
        responses = [
            {"role": "assistant", "tool_calls": [tool_call("search_1", "grep_search", {"pattern": "^def", "include": "*.txt"})]},
            {"role": "assistant", "tool_calls": [tool_call("read_1", "read_file", {"file_path": "demo.txt"})]},
            {"role": "assistant", "content": "这是一个问候函数。"},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "demo.txt").write_text('def greet(name):\n    return f"你好，{name}！"\n', encoding="utf-8")
            with (
                patch("personal_agent.tools.PROJECT_ROOT", root),
                patch("personal_agent.agent.execute_tool", wraps=execute_tool) as dispatch,
            ):
                reply = run_agent_loop(messages, scripted_model(responses, requests))
        self.assertEqual(reply, "这是一个问候函数。")
        self.assertEqual([len(request) for request in requests], [1, 3, 5])
        self.assertEqual(requests[1][-1]["tool_call_id"], "search_1")
        self.assertIn("demo.txt:1:def greet", requests[1][-1]["content"])
        self.assertEqual(requests[2][-1]["tool_call_id"], "read_1")
        self.assertIn("   2 |", requests[2][-1]["content"])
        self.assertEqual(dispatch.call_count, 2)
        self.assertEqual([message["role"] for message in messages], ["user", "assistant", "tool", "assistant", "tool", "assistant"])

    def test_multiple_calls_get_error_results_with_matching_ids(self):
        bad_json = tool_call("bad_json", "read_file", {})
        bad_json["function"]["arguments"] = "{"
        calls = [bad_json, tool_call("unknown_1", "unknown", {}), tool_call("raise_1", "read_file", {"file_path": "demo.txt"})]
        messages = [{"role": "user", "content": "错误演示"}]
        requests = []
        responses = [{"role": "assistant", "tool_calls": calls}, {"role": "assistant", "content": "收到错误结果。"}]
        handler = Mock(side_effect=RuntimeError("demo"))
        with patch.dict(TOOL_REGISTRY["read_file"], {"handler": handler}):
            run_agent_loop(messages, scripted_model(responses, requests))
        results = requests[1][2:]
        self.assertEqual([message["tool_call_id"] for message in results], ["bad_json", "unknown_1", "raise_1"])
        self.assertTrue(all(message["content"].startswith(TOOL_ERROR_PREFIX) for message in results))
        handler.assert_called_once()

    def test_limit_fills_results_without_executing(self):
        messages = [{"role": "user", "content": "次数上限演示"}]
        responses = [{"role": "assistant", "tool_calls": [tool_call("limit_1", "read_file", {"file_path": "demo.txt"})]}]
        with patch("personal_agent.agent.execute_tool") as dispatch:
            with self.assertRaisesRegex(RuntimeError, "上限"):
                run_agent_loop(messages, scripted_model(responses, []), max_turns=1)
        dispatch.assert_not_called()
        self.assertEqual(messages[-1]["tool_call_id"], "limit_1")
        self.assertIn("工具未执行", messages[-1]["content"])


if __name__ == "__main__":
    unittest.main()
