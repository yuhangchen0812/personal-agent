"""验证多轮交互和失败回滚，不调用真实模型。"""

import copy
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import httpx2
from openai import RateLimitError

from personal_agent.main import run_repl


class ChatTests(unittest.TestCase):
    def run_session(self, inputs, replies, max_turns=5):
        """提供模拟输入和回复，并记录每次请求时的历史快照。"""
        requests = []
        results = iter(replies)
        output = io.StringIO()
        errors = io.StringIO()

        def fake_model(messages, definitions, **_):
            requests.append(copy.deepcopy(messages))
            result = next(results)
            if isinstance(result, BaseException):
                raise result
            return result if isinstance(result, dict) else {"role": "assistant", "content": result}

        with (
            patch("builtins.input", side_effect=inputs),
            patch("personal_agent.main.get_model_message", side_effect=fake_model),
            patch("personal_agent.main.discover_skills", return_value=([], [])),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            history = run_repl(max_turns=max_turns)

        return history, requests, output.getvalue(), errors.getvalue()

    def test_history_sent_in_order(self):
        history, requests, output, errors = self.run_session(
            ["你好", "继续", "exit"], ["第一条回复", "第二条回复"]
        )
        expected = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "第一条回复"},
            {"role": "user", "content": "继续"},
            {"role": "assistant", "content": "第二条回复"},
        ]
        self.assertEqual(history, expected)
        self.assertEqual(requests, [expected[:1], expected[:3]])
        self.assertIn("Personal Agent > 第二条回复", output)
        self.assertEqual(errors, "")

    def test_blank_input_and_exit_do_not_call_model(self):
        history, requests, _, errors = self.run_session(
            ["", "  ", "ExIt"], []
        )
        self.assertEqual(history, [])
        self.assertEqual(requests, [])
        self.assertEqual(errors, "")

    def test_eof_without_input_returns_empty_history(self):
        history, requests, _, _ = self.run_session([EOFError()], [])
        self.assertEqual(history, [])
        self.assertEqual(requests, [])

    def test_eof_preserves_completed_turn(self):
        history, requests, _, _ = self.run_session(
            ["你好", EOFError()], ["收到"]
        )
        self.assertEqual(len(requests), 1)
        self.assertEqual(history, [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "收到"},
        ])

    def test_failed_turn_is_removed_before_next_request(self):
        history, requests, _, errors = self.run_session(
            ["第一问", "失败的问题", "下一问", "exit"],
            ["第一答", RuntimeError("模拟正文无效"), "下一答"],
        )
        expected = [
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "第一答"},
            {"role": "user", "content": "下一问"},
            {"role": "assistant", "content": "下一答"},
        ]
        self.assertEqual(history, expected)
        self.assertEqual(requests[2], expected[:3])
        self.assertEqual(requests[1][-1]["content"], "失败的问题")
        self.assertIn("本轮消息未保存", errors)

    def test_configuration_error_can_be_followed_by_another_turn(self):
        history, requests, _, errors = self.run_session(
            ["失败的问题", "成功的问题", "exit"],
            [ValueError("模拟配置缺失"), "成功的回复"],
        )
        self.assertEqual(history, [
            {"role": "user", "content": "成功的问题"},
            {"role": "assistant", "content": "成功的回复"},
        ])
        self.assertEqual(len(requests[1]), 1)
        self.assertIn("模拟配置缺失", errors)

    def test_sdk_error_rolls_back_without_printing_server_body(self):
        response = httpx2.Response(
            429, request=httpx2.Request("POST", "https://example.invalid/chat")
        )
        error = RateLimitError(
            "local-sensitive-server-detail", response=response, body={}
        )
        history, requests, output, errors = self.run_session(
            ["失败的问题", "下一问", "exit"], [error, "下一答"]
        )
        self.assertEqual(history, [
            {"role": "user", "content": "下一问"},
            {"role": "assistant", "content": "下一答"},
        ])
        self.assertEqual(len(requests[1]), 1)
        self.assertIn("429", errors)
        self.assertNotIn("local-sensitive-server-detail", output + errors)

    def test_interrupt_during_request_rolls_back(self):
        history, requests, output, errors = self.run_session(
            ["被中断的问题", "下一问", "exit"], [KeyboardInterrupt(), "下一答"]
        )
        self.assertEqual(history, [
            {"role": "user", "content": "下一问"},
            {"role": "assistant", "content": "下一答"},
        ])
        self.assertEqual(len(requests[1]), 1)
        self.assertIn("已中断本轮任务", output)
        self.assertEqual(errors, "")

    def test_interrupt_during_input_preserves_history(self):
        history, requests, _, _ = self.run_session(
            ["你好", KeyboardInterrupt()], ["收到"]
        )
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[-1], {"role": "assistant", "content": "收到"})

    def test_new_run_has_fresh_history(self):
        first, _, _, _ = self.run_session(["第一段", "exit"], ["第一答"])
        second, requests, _, _ = self.run_session(["第二段", "exit"], ["第二答"])
        self.assertIsNot(first, second)
        self.assertEqual(requests, [[{"role": "user", "content": "第二段"}]])
        self.assertEqual(first[-1]["content"], "第一答")

    def test_failure_after_tool_result_rolls_back_whole_turn(self):
        tool_request = {
            "role": "assistant",
            "tool_calls": [{"id": "failed_turn_1", "type": "function", "function": {"name": "unknown", "arguments": "{}"}}],
        }
        history, requests, _, errors = self.run_session(
            ["第一问", "失败的问题", "下一问", "exit"],
            ["第一答", tool_request, RuntimeError("模拟后续请求失败"), "下一答"],
        )
        self.assertEqual(requests[2][-1]["tool_call_id"], "failed_turn_1")
        self.assertEqual(requests[3], [
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "第一答"},
            {"role": "user", "content": "下一问"},
        ])
        self.assertEqual([message["role"] for message in history], ["user", "assistant", "user", "assistant"])
        self.assertIn("本轮消息未保存", errors)

    def test_turn_limit_rolls_back_and_next_input_can_succeed(self):
        tool_request = {
            "role": "assistant",
            "tool_calls": [{"id": "limited_1", "type": "function", "function": {"name": "list_files", "arguments": '{"pattern":"*.py"}'}}],
        }
        with patch("personal_agent.agent.execute_tool") as dispatch:
            history, requests, output, errors = self.run_session(
                ["第一问", "需要工具的问题", "下一问", "exit"],
                ["第一答", tool_request, "下一答"],
                max_turns=1,
            )
        dispatch.assert_not_called()
        self.assertIn("上限（1 次）", errors)
        self.assertEqual(requests[2], [
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "第一答"},
            {"role": "user", "content": "下一问"},
        ])
        self.assertEqual(history[-1], {"role": "assistant", "content": "下一答"})
        self.assertEqual(len(history), 4)
        self.assertIn("每轮父任务及每个子任务各最多请求模型 1 次", output)
