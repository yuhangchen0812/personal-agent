"""一条 explore 主流程：独立历史、实际读取、执行侧限制和结果返回。"""

import copy
import json
import tempfile
import unittest
from functools import partial
from pathlib import Path
from unittest.mock import Mock, patch

from personal_agent.subagent import EXPLORE_TOOLS, run_explore_agent
from personal_agent.tool_registry import TOOL_REGISTRY, execute_tool


def tool_call(call_id, name, arguments):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


class SubAgentSmokeTest(unittest.TestCase):
    def test_explore_isolates_history_and_enforces_read_only_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "source.txt").write_text("局部调查证据\n", encoding="utf-8")
            target = root / "playground/keep.txt"
            target.parent.mkdir()
            target.write_bytes(b"keep original")
            original = target.read_bytes()
            parent_messages = [{"role": "user", "content": "父级私有历史，不传入子任务。"}]
            parent_before = copy.deepcopy(parent_messages)
            forbidden = {
                "write_file": {"file_path": "playground/keep.txt", "content": "不应写入"},
                "edit_file": {"file_path": "playground/keep.txt", "old_string": "keep", "new_string": "changed"},
                "run_shell": {"command": "Write-Output '不应执行'"},
                "skill": {"skill_name": "code-review"},
                "enter_plan_mode": {}, "exit_plan_mode": {}, "compact_context": {},
            }
            calls = [
                tool_call("read", "read_file", {"file_path": "source.txt"}),
                tool_call("denied_search", "grep_search", {"pattern": "证据"}),
                *[tool_call(name, name, arguments) for name, arguments in forbidden.items()],
            ]
            responses = iter([
                {"role": "assistant", "tool_calls": calls},
                {"role": "assistant", "content": "根据 source.txt：局部调查证据；搜索及其他操作被拒绝。"},
            ])
            requests = []

            def model(messages, definitions):
                requests.append(copy.deepcopy(messages))
                self.assertEqual({item["name"] for item in definitions}, EXPLORE_TOOLS)
                return next(responses)

            # 调用方使用 bypassPermissions；explore 仍必须拒绝所有非白名单操作。
            dispatch = Mock(wraps=partial(execute_tool, mode="bypassPermissions", deny=["grep_search"]))
            blocked_handler = Mock(side_effect=AssertionError("非只读处理函数不应执行"))
            with (
                patch("personal_agent.tools.PROJECT_ROOT", root),
                patch.dict(TOOL_REGISTRY["write_file"], {"handler": blocked_handler}),
                patch.dict(TOOL_REGISTRY["edit_file"], {"handler": blocked_handler}),
                patch.dict(TOOL_REGISTRY["run_shell"], {"handler": blocked_handler}),
            ):
                result = run_explore_agent("读取 source.txt 中的证据", model, dispatch, max_turns=2)
                self.assertEqual(result.text, result.messages[-1]["content"])
                self.assertEqual(parent_messages, parent_before)
                self.assertNotIn("父级私有历史", str(requests))
                self.assertEqual([item["role"] for item in requests[0]], ["system", "user"])
                results = {item["tool_call_id"]: item["content"] for item in result.messages if item["role"] == "tool"}
                self.assertIn("局部调查证据", results["read"])
                self.assertIn("显式 deny", results["denied_search"])
                for name in forbidden:
                    self.assertIn("explore 仅允许", results[name])
                self.assertEqual(dispatch.call_count, 2)
                blocked_handler.assert_not_called()
                self.assertEqual(target.read_bytes(), original)
                self.assertEqual([item["tool_call_id"] for item in requests[1] if item["role"] == "tool"], [call["id"] for call in calls])

                def second_model(messages, definitions):
                    self.assertEqual(len(messages), 2)
                    self.assertEqual(messages[-1]["content"], "第二个独立任务")
                    return {"role": "assistant", "content": "第二个结果"}

                second = run_explore_agent("第二个独立任务", second_model, dispatch)
                self.assertIsNot(second.messages, result.messages)
                self.assertEqual(len(second.messages), 3)
                self.assertEqual(len(requests), 2)

                limit_model = Mock(return_value={"role": "assistant", "tool_calls": [calls[0]]})
                no_dispatch = Mock()
                with self.assertRaisesRegex(RuntimeError, "上限"):
                    run_explore_agent("验证子任务上限", limit_model, no_dispatch, max_turns=1)
                no_dispatch.assert_not_called()
                failed_model = Mock(side_effect=RuntimeError("模拟模型失败"))
                with self.assertRaisesRegex(RuntimeError, "模拟模型失败"):
                    run_explore_agent("失败任务", failed_model, dispatch)
                self.assertEqual(parent_messages, parent_before)
                with self.assertRaises(ValueError):
                    run_explore_agent(" ", failed_model, dispatch)
