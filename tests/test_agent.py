"""Agent Loop 的三个核心流程测试，不请求真实模型。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from personal_agent.agent import run_agent_loop
from personal_agent.tool_registry import TOOL_ERROR_PREFIX, TOOL_REGISTRY, execute_tool
from personal_agent.workspace import tool_workspace


def tool_call(call_id, name, arguments):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def scripted_model(responses, requests):
    responses = iter(responses)

    def call_model(messages, definitions):
        requests.append(copy.deepcopy(messages))
        return next(responses)

    return call_model


class AgentLoopTests(unittest.TestCase):
    def test_parallel_reads_inherit_scoped_workspace_instead_of_outer_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            outer = Path(temporary).resolve()
            scoped = outer / "scoped"
            scoped.mkdir()
            for index in (1, 2):
                (outer / f"file-{index}.txt").write_text("outer-project-data", encoding="utf-8")
                (scoped / f"file-{index}.txt").write_text(f"scoped-{index}", encoding="utf-8")
            messages = [{"role": "user", "content": "read scoped files"}]
            responses = [
                {"role": "assistant", "tool_calls": [tool_call(str(index), "read_file", {"file_path": f"file-{index}.txt"}) for index in (1, 2)]},
                {"role": "assistant", "content": "done"},
            ]
            with patch("personal_agent.tools.PROJECT_ROOT", outer), tool_workspace(scoped):
                self.assertEqual(run_agent_loop(messages, scripted_model(responses, []), max_parallel_reads=2), "done")
            results = [message["content"] for message in messages if message["role"] == "tool"]
            self.assertEqual(results, ["   1 | scoped-1", "   1 | scoped-2"])
            self.assertNotIn("outer-project-data", str(messages))

    def test_invalid_tool_call_container_does_not_change_history_or_execute(self):
        for invalid in (None, {}, False, 0, ""):
            with self.subTest(tool_calls=invalid):
                messages = [{"role": "user", "content": "invalid response"}]
                before = copy.deepcopy(messages)
                dispatch = Mock()
                response = {"role": "assistant", "content": "must not be accepted", "tool_calls": invalid}
                with self.assertRaisesRegex(ValueError, "tool_calls"):
                    run_agent_loop(messages, scripted_model([response], []), tool_executor=dispatch)
                self.assertEqual(messages, before)
                dispatch.assert_not_called()

    def test_whitespace_call_id_is_rejected_before_any_tool_runs(self):
        messages = [{"role": "user", "content": "invalid ID"}]
        dispatch = Mock()
        response = {"role": "assistant", "tool_calls": [tool_call("  ", "read_file", {"file_path": "file.txt"})]}
        with self.assertRaisesRegex(ValueError, "ID"):
            run_agent_loop(messages, scripted_model([response], []), tool_executor=dispatch)
        self.assertEqual(len(messages), 1)
        dispatch.assert_not_called()

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
