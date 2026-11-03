"""自定义委派两条主链路：快照/空工具，以及父权限/共享预算。"""

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
from personal_agent.session import Session
from personal_agent.subagent import GENERAL_TOOLS
from personal_agent.tool_registry import TOOL_REGISTRY, execute_tool, get_tool_definitions
from personal_agent.tools import read_file, write_file
from personal_agent.usage import TokenPrices


def call(call_id, name, arguments):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


class CustomAgentSmokeTests(unittest.TestCase):
    def test_custom_snapshot_refresh_and_explicit_empty_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            directory = root / ".personal/agents"
            directory.mkdir(parents=True)
            path = directory / "different-file.md"
            path.write_text("---\nname: code-reader\ndescription: 读取代码\nallowed-tools: read_file, grep_search\n---\nPRIVATE_CUSTOM_V1", encoding="utf-8")
            (directory / "general.md").write_text("BUILTIN_OVERRIDE_MUST_NOT_LOAD", encoding="utf-8")
            (root / "source.txt").write_text("真实子任务证据", encoding="utf-8")
            counts = {"parent": 0, "child": 0}
            captures = []

            def model(messages, definitions, *, on_usage):
                child = any("PRIVATE_CUSTOM_" in str(item.get("content")) for item in messages)
                role = "child" if child else "parent"
                counts[role] += 1
                captures.append((role, copy.deepcopy(messages), copy.deepcopy(definitions)))
                on_usage({"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12})
                if not child:
                    agent = next(item for item in definitions if item["name"] == "agent")
                    self.assertEqual(agent["input_schema"]["properties"]["type"]["enum"], ["explore", "plan", "general", "code-reader"])
                    self.assertNotIn("PRIVATE_CUSTOM_", str(messages) + str(definitions))
                    self.assertNotIn("BUILTIN_OVERRIDE", str(messages) + str(definitions))
                    self.assertIn("PARENT_MEMORY", str(messages))
                    tools = '["read_file", "grep_search"]' if counts[role] <= 2 else '[]'
                    self.assertIn('"tools": ' + tools, agent["description"])
                    if counts[role] in (1, 3):
                        if counts[role] == 1:
                            # 请求发出后改文件，本轮仍须使用已经发现的旧正文与工具范围。
                            path.write_text("---\nname: code-reader\ndescription: 只回答\nallowed-tools: []\n---\nPRIVATE_CUSTOM_V2", encoding="utf-8")
                        return {"role": "assistant", "tool_calls": [call(f"custom_{counts[role]}", "agent", {"type": "code-reader", "prompt": "调查 source.txt"})]}
                    self.assertEqual(messages[-1]["content"], "自定义结果")
                    self.assertNotIn("child_read", str(messages))
                    return {"role": "assistant", "content": "父任务完成"}
                self.assertNotIn("PRIVATE_PARENT", str(messages))
                self.assertNotIn("PARENT_MEMORY", str(messages))
                if counts[role] in (1, 3):
                    self.assertEqual([item["role"] for item in messages], ["system", "user"])
                    self.assertIn("PRIVATE_CUSTOM_V1" if counts[role] == 1 else "PRIVATE_CUSTOM_V2", messages[0]["content"])
                    self.assertEqual({item["name"] for item in definitions}, {"read_file", "grep_search"} if counts[role] == 1 else set())
                    return {"role": "assistant", "tool_calls": [
                        call("child_read", "read_file", {"file_path": "source.txt"}),
                        call("child_search", "grep_search", {"pattern": "证据"}),
                        call("child_write", "write_file", {"file_path": "playground/blocked.txt", "content": "不能写"}),
                        call("recursive", "agent", {"prompt": "不能递归"}),
                    ]}
                results = {item["tool_call_id"]: item["content"] for item in messages if item["role"] == "tool"}
                if counts[role] == 2:
                    self.assertIn("真实子任务证据", results["child_read"])
                    self.assertIn("显式 deny", results["child_search"])
                else:
                    self.assertTrue(all("code-reader 仅允许" in value for value in results.values()))
                self.assertIn("code-reader 仅允许", results["child_write"])
                self.assertIn("code-reader 仅允许", results["recursive"])
                return {"role": "assistant", "content": "自定义结果"}

            output, errors = io.StringIO(), io.StringIO()
            reader, blocked = Mock(wraps=read_file), Mock()
            with (
                patch("personal_agent.agent_config.PROJECT_ROOT", root),
                patch("personal_agent.agent_config.USER_AGENTS_ROOT", root / "user-agents"),
                patch("personal_agent.tools.PROJECT_ROOT", root),
                patch("personal_agent.memory.PROJECT_ROOT", root),
                patch("personal_agent.skills.PROJECT_ROOT", root),
                patch("personal_agent.skills.USER_SKILLS_ROOT", root / "user-skills"),
                patch("personal_agent.session.PROJECT_ROOT", root),
                patch.dict(TOOL_REGISTRY["read_file"], {"handler": reader}),
                patch.dict(TOOL_REGISTRY["write_file"], {"handler": blocked}),
                patch.dict(TOOL_REGISTRY["grep_search"], {"handler": blocked}),
                patch("personal_agent.main.get_model_message", side_effect=model),
                patch("personal_agent.main.confirm_in_terminal") as confirm,
                redirect_stdout(output), redirect_stderr(errors),
            ):
                session = Session("custom", with_tools=True)
                session.create()
                original = [{"role": "user", "content": "PRIVATE_PARENT"}, {"role": "assistant", "content": "已记录"}]
                session.save(original)
                with patch("builtins.input", side_effect=["/memory save profile PARENT_MEMORY", "/memory use profile", "首次委派", "刷新后再次委派", "/usage", "exit"]):
                    history = run_repl(session_name="custom", resume=True, permission_mode="bypassPermissions", deny=["grep_search"])
                self.assertEqual(session.load(), history)
                self.assertEqual(history[:2], original)
                self.assertEqual(len(history), 10)
                with patch("builtins.input", side_effect=["exit"]):
                    self.assertEqual(run_repl(session_name="custom", resume=True), history)
                confirm.assert_not_called()
            self.assertEqual(counts, {"parent": 4, "child": 4})
            self.assertEqual(reader.call_count, 1)
            blocked.assert_not_called()
            self.assertEqual(errors.getvalue(), "")
            self.assertIn("逻辑调用 8 次，完整用量 8 次", output.getvalue())
            static = next(item for item in get_tool_definitions() if item["name"] == "agent")
            self.assertEqual(static["input_schema"]["properties"]["type"]["enum"], ["explore", "plan", "general"])

    def test_missing_tools_defaults_still_use_parent_approval_plan_and_budget(self):
        delegate = Mock()
        self.assertIn("参数 type", execute_tool("agent", {"type": "helper", "prompt": "任务"}, delegate=delegate))
        self.assertIn("权限拒绝", execute_tool("agent", {"type": "helper", "prompt": "任务"}, delegate=delegate, agent_types=["helper"], deny=["agent"]))
        delegate.assert_not_called()
        for scenario in ("default", "plan", "budget"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                path = root / ".personal/agents/helper.md"
                path.parent.mkdir(parents=True)
                path.write_text("---\ndescription: 局部任务\n---\nDEFAULT_CUSTOM_BODY", encoding="utf-8")
                counts = {"parent": 0, "child": 0}

                def model(messages, definitions, *, on_usage):
                    child = any("DEFAULT_CUSTOM_BODY" in str(item.get("content")) for item in messages)
                    role = "child" if child else "parent"
                    counts[role] += 1
                    on_usage({"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1})
                    if not child:
                        if counts[role] == 1:
                            return {"role": "assistant", "tool_calls": [call("helper", "agent", {"type": "helper", "prompt": "写入结果"})]}
                        return {"role": "assistant", "content": "父任务结束"}
                    self.assertEqual({item["name"] for item in definitions}, GENERAL_TOOLS)
                    if counts[role] == 1:
                        return {"role": "assistant", "tool_calls": [call("write", "write_file", {"file_path": "playground/result.txt", "content": "获批结果"})]}
                    self.assertIn("权限拒绝" if scenario == "plan" else "已创建", messages[-1]["content"])
                    return {"role": "assistant", "content": "子任务结束"}

                writer = Mock(wraps=write_file)
                output, errors = io.StringIO(), io.StringIO()
                with (
                    patch("personal_agent.agent_config.PROJECT_ROOT", root),
                    patch("personal_agent.agent_config.USER_AGENTS_ROOT", root / "user-agents"),
                    patch("personal_agent.tools.PROJECT_ROOT", root),
                    patch("personal_agent.skills.PROJECT_ROOT", root),
                    patch("personal_agent.skills.USER_SKILLS_ROOT", root / "user-skills"),
                    patch.dict(TOOL_REGISTRY["write_file"], {"handler": writer}),
                    patch("personal_agent.main.get_model_message", side_effect=model),
                    patch("personal_agent.main.confirm_in_terminal", return_value=True) as confirm,
                    patch("builtins.input", side_effect=["委派 helper", "/usage", "exit"]),
                    redirect_stdout(output), redirect_stderr(errors),
                ):
                    history = run_repl(
                        permission_mode="plan" if scenario == "plan" else "default",
                        allow=["write_file"] if scenario == "plan" else None,
                        prices=TokenPrices(Decimal(1_000_000), Decimal(1_000_000)),
                        max_cost=Decimal(2 if scenario == "budget" else 10),
                    )
                target = root / "playground/result.txt"
                if scenario == "default":
                    self.assertEqual(target.read_text(encoding="utf-8"), "获批结果")
                    confirm.assert_called_once()
                    writer.assert_called_once()
                else:
                    self.assertFalse(target.exists())
                    confirm.assert_not_called()
                    writer.assert_not_called()
                if scenario == "budget":
                    self.assertEqual(history, [])
                    self.assertEqual(counts, {"parent": 1, "child": 1})
                    self.assertIn("已达到本次运行估算费用预算", errors.getvalue())
                    self.assertIn("逻辑调用 2 次，完整用量 2 次", output.getvalue())
                else:
                    self.assertEqual(counts, {"parent": 2, "child": 2})
                    self.assertEqual(len(history), 4)
                    self.assertEqual(errors.getvalue(), "")
