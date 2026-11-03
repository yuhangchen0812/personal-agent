"""父级委派的两条主链路：独立上下文/结果回填，以及共享用量预算。"""

import copy
import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from personal_agent.main import run_repl
from personal_agent.permissions import PERMISSION_MODES, READ_TOOLS, check_permission
from personal_agent.session import Session
from personal_agent.tool_registry import TOOL_ERROR_PREFIX, TOOL_REGISTRY, execute_tool
from personal_agent.tools import read_file
from personal_agent.usage import TokenPrices


def call(call_id, name, arguments):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


class AgentToolSmokeTests(unittest.TestCase):
    def test_parent_delegates_with_isolated_context_and_original_permissions(self):
        delegate = Mock(return_value="不应执行")
        for arguments, options in [
            ({}, {}), ({"prompt": "调查", "type": "unknown"}, {}),
            ({"prompt": "调查", "permission_mode": "bypassPermissions"}, {}),
            ({"prompt": "调查"}, {"deny": ["agent"]}),
        ]:
            self.assertTrue(execute_tool("agent", arguments, delegate=delegate, **options).startswith(TOOL_ERROR_PREFIX))
        delegate.assert_not_called()
        self.assertIn("运行时", execute_tool("agent", {"prompt": "调查"}))
        self.assertNotIn("agent", READ_TOOLS)
        for mode in PERMISSION_MODES:
            self.assertEqual(check_permission("agent", mode)["action"], "allow")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "source.txt").write_text("子任务读取的实际证据", encoding="utf-8")
            skill = root / ".personal/skills/check-flow/SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("---\nname: check-flow\ndescription: 调查解析入口\n---\n技能正文", encoding="utf-8")
            prefix = [{"role": "user", "content": "父级旧历史标记"}, {"role": "assistant", "content": "已记录"}]
            parent_requests, child_requests = [], []
            thread_id = threading.get_ident()
            usage = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}

            def parent_model(messages, definitions, *, on_usage, on_text):
                parent_requests.append(copy.deepcopy(messages))
                on_usage(usage)
                self.assertIn("agent", {tool["name"] for tool in definitions})
                if len(parent_requests) == 1:
                    self.assertIn("PARENT_MEMORY", str(messages))
                    self.assertIn("<retrieved_skills>", str(messages))
                    return {"role": "assistant", "tool_calls": [call("delegation_1", "agent", {"prompt": "读取 source.txt 并返回结论"})]}
                result = messages[-1]
                self.assertEqual(result, {"role": "tool", "tool_call_id": "delegation_1", "content": "子任务最终结论"})
                self.assertNotIn("child_read", str(messages))
                self.assertNotIn("子任务读取的实际证据", str(messages))
                on_text("父任务最终回答")
                return {"role": "assistant", "content": "父任务最终回答"}

            def child_model(messages, definitions, *, on_usage):
                self.assertEqual(threading.get_ident(), thread_id)
                child_requests.append(copy.deepcopy(messages))
                on_usage(usage)
                self.assertEqual({tool["name"] for tool in definitions}, {"read_file", "list_files", "grep_search"})
                for excluded in ("PARENT_MEMORY", "父级旧历史标记", "<retrieved_skills>", "Plan 状态"):
                    self.assertNotIn(excluded, str(messages))
                if len(child_requests) == 1:
                    self.assertEqual([item["role"] for item in messages], ["system", "user"])
                    return {"role": "assistant", "tool_calls": [
                        call("child_read", "read_file", {"file_path": "source.txt"}),
                        call("child_denied", "grep_search", {"pattern": "证据"}),
                        call("child_write", "write_file", {"file_path": "playground/blocked.txt", "content": "不能写"}),
                        call("child_recursive", "agent", {"prompt": "不能递归"}),
                    ]}
                results = {item["tool_call_id"]: item["content"] for item in messages if item["role"] == "tool"}
                self.assertIn("子任务读取的实际证据", results["child_read"])
                self.assertIn("显式 deny", results["child_denied"])
                self.assertIn("explore 仅允许", results["child_write"])
                self.assertIn("explore 仅允许", results["child_recursive"])
                return {"role": "assistant", "content": "子任务最终结论"}

            blocked = Mock()
            output, errors = io.StringIO(), io.StringIO()
            with (
                patch("personal_agent.tools.PROJECT_ROOT", root),
                patch("personal_agent.memory.PROJECT_ROOT", root),
                patch("personal_agent.skills.PROJECT_ROOT", root),
                patch("personal_agent.agent_config.PROJECT_ROOT", root),
                patch("personal_agent.agent_config.USER_AGENTS_ROOT", root / "user-agents"),
                patch("personal_agent.skills.USER_SKILLS_ROOT", root / "user-skills"),
                patch("personal_agent.session.PROJECT_ROOT", root),
                patch.dict(TOOL_REGISTRY["write_file"], {"handler": blocked}),
                patch.dict(TOOL_REGISTRY["grep_search"], {"handler": blocked}),
                patch("personal_agent.main.get_model_message", side_effect=child_model) as child,
                patch("personal_agent.main.stream_model_message", side_effect=parent_model) as parent,
                patch("personal_agent.main.confirm_in_terminal") as confirm,
                redirect_stdout(output), redirect_stderr(errors),
            ):
                session = Session("delegate", with_tools=True)
                session.create()
                session.save(prefix)
                with patch("builtins.input", side_effect=[
                    "/memory save profile PARENT_MEMORY", "/memory use profile",
                    "调查解析入口，请委派子任务读取 source.txt", "/usage", "/cost", "exit",
                ]):
                    history = run_repl(
                        session_name="delegate", resume=True, stream=True, permission_mode="plan",
                        deny=["grep_search"], max_turns=3,
                        prices=TokenPrices(Decimal(2), Decimal(8)), max_cost=Decimal("0.01"),
                    )
                self.assertEqual(history[:2], prefix)
                self.assertEqual(Session("delegate").load(), history)
                self.assertEqual(len(history), 6)
                with patch("builtins.input", side_effect=["exit"]):
                    self.assertEqual(run_repl(session_name="delegate", resume=True), history)
                self.assertEqual((parent.call_count, child.call_count), (2, 2))
                confirm.assert_not_called()
                blocked.assert_not_called()
            self.assertFalse((root / "playground/blocked.txt").exists())
            self.assertEqual(errors.getvalue(), "")
            self.assertIn("逻辑调用 4 次，完整用量 4 次，缺失 0 次", output.getvalue())
            self.assertIn("输入 40，输出 8，总计 48", output.getvalue())
            self.assertIn("已报告用量估算费用：0.000144 元", output.getvalue())

    def test_child_usage_stops_later_tools_or_parent_requests(self):
        # 单价为 1 元/token，便于核对父子共用的费用累计。
        prices = TokenPrices(Decimal(1_000_000), Decimal(1_000_000))
        for scenario, expected_calls, expected_reads, reason in [
            ("before_tools", 2, 0, "已达到本次运行估算费用预算"),
            ("plan_before_tools", 2, 0, "已达到本次运行估算费用预算"),
            ("general_before_tools", 2, 0, "已达到本次运行估算费用预算"),
            ("after_child", 3, 1, "已达到本次运行估算费用预算"),
            ("missing", 2, 0, "缺少完整用量"),
        ]:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                (root / "source.txt").write_text("预算内读取", encoding="utf-8")
                requests = []

                def model(messages, definitions, *, on_usage):
                    requests.append(copy.deepcopy(messages))
                    number = len(requests)
                    amount = 2 if (scenario.endswith("before_tools") and number == 2) or number == 3 else 1
                    on_usage(None if scenario == "missing" and number == 2 else {
                        "prompt_tokens": amount, "completion_tokens": 0, "total_tokens": amount,
                    })
                    if number == 1:
                        arguments = {"prompt": "读取 source.txt"}
                        if scenario in {"plan_before_tools", "general_before_tools"}:
                            arguments["type"] = scenario.split("_", 1)[0]
                        return {"role": "assistant", "tool_calls": [call("delegation", "agent", arguments)]}
                    if number == 2 and scenario != "missing":
                        if scenario == "general_before_tools":
                            return {"role": "assistant", "tool_calls": [call("write", "write_file", {"file_path": "playground/blocked.txt", "content": "预算不足不能写"})]}
                        return {"role": "assistant", "tool_calls": [call("read", "read_file", {"file_path": "source.txt"})]}
                    return {"role": "assistant", "content": "子任务完成，不能再发父级请求"}

                reader = Mock(wraps=read_file)
                output, errors = io.StringIO(), io.StringIO()
                with (
                    patch("personal_agent.tools.PROJECT_ROOT", root),
                    patch("personal_agent.skills.PROJECT_ROOT", root),
                    patch("personal_agent.agent_config.PROJECT_ROOT", root),
                    patch("personal_agent.agent_config.USER_AGENTS_ROOT", root / "user-agents"),
                    patch("personal_agent.skills.USER_SKILLS_ROOT", root / "user-skills"),
                    patch.dict(TOOL_REGISTRY["read_file"], {"handler": reader}),
                    patch("personal_agent.main.get_model_message", side_effect=model) as request,
                    patch("personal_agent.main.confirm_in_terminal", return_value=True) as confirm,
                    patch("builtins.input", side_effect=["委派只读调查", "继续", "/usage", "/cost", "exit"]),
                    redirect_stdout(output), redirect_stderr(errors),
                ):
                    history = run_repl(prices=prices, max_cost=Decimal(4 if scenario == "after_child" else 3))
                self.assertEqual(history, [])
                self.assertEqual(request.call_count, expected_calls)
                self.assertEqual(reader.call_count, expected_reads)
                confirm.assert_not_called()
                self.assertFalse((root / "playground/blocked.txt").exists())
                self.assertIn(reason, errors.getvalue())
                self.assertIn(f"逻辑调用 {expected_calls} 次", output.getvalue())
                if scenario == "missing":
                    self.assertIn("完整用量 1 次，缺失 1 次", output.getvalue())
                else:
                    self.assertIn(f"已报告用量估算费用：{4 if scenario == 'after_child' else 3}.000000 元", output.getvalue())
