"""两条 plan 委派链路：只读结构化计划，以及无效格式的错误回填。"""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from personal_agent.main import run_repl
from personal_agent.plan import PlanSession
from personal_agent.session import Session
from personal_agent.subagent import PLAN_SYSTEM_PROMPT, PlanFormatError, parse_plan_result
from personal_agent.tool_registry import TOOL_REGISTRY, execute_tool
from personal_agent.usage import TokenPrices


def call(call_id, name, arguments):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


class PlanAgentSmokeTests(unittest.TestCase):
    def test_parent_receives_plan_without_changing_plan_session(self):
        delegate = Mock(return_value="计划结果")
        self.assertEqual(execute_tool("agent", {"prompt": "设计方案", "type": "plan"}, delegate=delegate), "计划结果")
        delegate.assert_called_once_with("设计方案", "plan")
        delegate.reset_mock()
        self.assertIn("权限拒绝", execute_tool("agent", {"prompt": "设计方案", "type": "plan"}, deny=["agent"], delegate=delegate))
        delegate.assert_not_called()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "source.py").write_text("def format_line():\n    return 'old'\n", encoding="utf-8")
            target = root / "playground/keep.txt"
            target.parent.mkdir()
            target.write_bytes(b"keep")
            plan = {
                "summary": "source.py:1 定义 format_line，目前返回 old；建议补充输入参数。",
                "steps": ["为 format_line 增加参数并保留默认行为。", "用默认值和自定义值验证返回内容。"],
                "files": ["source.py:1-2：已读取函数定义与返回值。"],
                "risks": ["调用方兼容性尚未调查，需要先搜索引用。"],
            }
            requests, states = [], []
            counts = {"parent": 0, "plan": 0}

            def make_plan_session(mode):
                session = PlanSession(mode)
                states.append((session, session.snapshot()))
                return session

            def model(messages, definitions, *, on_usage):
                child = any(item.get("content") == PLAN_SYSTEM_PROMPT for item in messages)
                role = "plan" if child else "parent"
                counts[role] += 1
                requests.append((role, copy.deepcopy(messages)))
                on_usage({"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12})
                if not child:
                    if counts[role] == 1:
                        schema = next(item["input_schema"] for item in definitions if item["name"] == "agent")
                        self.assertEqual(schema["properties"]["type"]["enum"], ["explore", "plan", "general"])
                        return {"role": "assistant", "tool_calls": [call("plan_task", "agent", {"prompt": "调查 source.py 后设计参数化方案", "type": "plan"})]}
                    self.assertEqual(messages[-1]["tool_call_id"], "plan_task")
                    self.assertEqual(parse_plan_result(messages[-1]["content"]), plan)
                    self.assertNotIn("plan_read", str(messages))
                    return {"role": "assistant", "content": "已取得计划，尚未批准或执行。"}
                self.assertEqual({item["name"] for item in definitions}, {"read_file", "list_files", "grep_search"})
                if counts[role] == 1:
                    self.assertEqual([item["role"] for item in messages], ["system", "user"])
                    return {"role": "assistant", "tool_calls": [
                        call("plan_read", "read_file", {"file_path": "source.py"}),
                        call("plan_denied", "grep_search", {"pattern": "format_line"}),
                        call("plan_write", "write_file", {"file_path": "playground/keep.txt", "content": "不能改"}),
                        call("plan_approve", "exit_plan_mode", {}),
                        call("plan_recursive", "agent", {"prompt": "不能递归"}),
                    ]}
                results = {item["tool_call_id"]: item["content"] for item in messages if item["role"] == "tool"}
                self.assertIn("def format_line", results["plan_read"])
                self.assertIn("显式 deny", results["plan_denied"])
                for key in ("plan_write", "plan_approve", "plan_recursive"):
                    self.assertIn("plan 仅允许", results[key])
                return {"role": "assistant", "content": json.dumps(plan, ensure_ascii=False)}

            output, errors = io.StringIO(), io.StringIO()
            blocked = Mock()
            with (
                patch("personal_agent.tools.PROJECT_ROOT", root),
                patch("personal_agent.skills.PROJECT_ROOT", root),
                patch("personal_agent.agent_config.PROJECT_ROOT", root),
                patch("personal_agent.agent_config.USER_AGENTS_ROOT", root / "user-agents"),
                patch("personal_agent.skills.USER_SKILLS_ROOT", root / "user-skills"),
                patch("personal_agent.session.PROJECT_ROOT", root),
                patch("personal_agent.main.PlanSession", side_effect=make_plan_session),
                patch("personal_agent.main.get_model_message", side_effect=model),
                patch("personal_agent.main.confirm_in_terminal") as confirm,
                patch.dict(TOOL_REGISTRY["write_file"], {"handler": blocked}),
                patch.dict(TOOL_REGISTRY["grep_search"], {"handler": blocked}),
                patch("builtins.input", side_effect=["请委派 plan 设计方案", "/usage", "exit"]),
                redirect_stdout(output), redirect_stderr(errors),
            ):
                history = run_repl(
                    session_name="plan_agent", with_tools=True, permission_mode="plan",
                    allow=["write_file"], deny=["grep_search"], max_turns=3,
                    prices=TokenPrices(Decimal(2), Decimal(8)), max_cost=Decimal("0.01"),
                )
                self.assertEqual(Session("plan_agent").load(), history)
                self.assertEqual(states[0][0].snapshot(), states[0][1])
                self.assertEqual(states[0][0].state, "planning")
                confirm.assert_not_called()
                blocked.assert_not_called()
            self.assertEqual(target.read_bytes(), b"keep")
            self.assertEqual(counts, {"parent": 2, "plan": 2})
            self.assertEqual(len(history), 4)
            self.assertEqual(errors.getvalue(), "")
            self.assertIn("逻辑调用 4 次，完整用量 4 次，缺失 0 次", output.getvalue())

    def test_invalid_plan_returns_tool_error_without_retry_or_lost_usage(self):
        valid = {"summary": "待调查", "steps": ["先读取实现"], "files": [], "risks": []}
        self.assertEqual(parse_plan_result(json.dumps(valid)), valid)
        for text in ("不是 JSON", "{}", json.dumps(valid | {"steps": []}), json.dumps(valid | {"files": [1]})):
            with self.assertRaises(PlanFormatError):
                parse_plan_result(text)

        requests = []

        def model(messages, definitions, *, on_usage):
            requests.append(copy.deepcopy(messages))
            on_usage({"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12})
            if len(requests) == 1:
                return {"role": "assistant", "tool_calls": [call("bad_plan", "agent", {"prompt": "设计方案", "type": "plan"})]}
            if len(requests) == 2:
                self.assertEqual(messages[0]["content"], PLAN_SYSTEM_PROMPT)
                return {"role": "assistant", "content": "```json\n" + json.dumps(valid) + "\n```"}
            self.assertEqual(messages[-1]["tool_call_id"], "bad_plan")
            self.assertIn("plan 子任务输出格式无效", messages[-1]["content"])
            self.assertNotIn("```", messages[-1]["content"])
            return {"role": "assistant", "content": "计划格式未通过校验，尚未取得有效计划。"}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            output, errors = io.StringIO(), io.StringIO()
            with (
                patch("personal_agent.skills.PROJECT_ROOT", root),
                patch("personal_agent.agent_config.PROJECT_ROOT", root),
                patch("personal_agent.agent_config.USER_AGENTS_ROOT", root / "user-agents"),
                patch("personal_agent.skills.USER_SKILLS_ROOT", root / "user-skills"),
                patch("personal_agent.main.get_model_message", side_effect=model) as request,
                patch("builtins.input", side_effect=["请求计划", "/usage", "/cost", "exit"]),
                redirect_stdout(output), redirect_stderr(errors),
            ):
                history = run_repl(prices=TokenPrices(Decimal(2), Decimal(8)), max_cost=Decimal("0.01"))
            self.assertEqual(request.call_count, 3)
            self.assertEqual(len(history), 4)
            self.assertEqual(errors.getvalue(), "")
            self.assertIn("逻辑调用 3 次，完整用量 3 次，缺失 0 次", output.getvalue())
            self.assertIn("已报告用量估算费用：0.000108 元", output.getvalue())
