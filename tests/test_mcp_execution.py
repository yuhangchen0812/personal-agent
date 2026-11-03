"""三条主流程：真实调用与错误、权限边界、聊天存档及恢复不重放。"""

import io
import json
import re
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from personal_agent.main import run_repl
from personal_agent.mcp_client import PROJECT_ROOT, McpProtocolError, McpRemoteError, StdioMcpClient
from personal_agent.mcp_runtime import McpToolRuntime, load_mcp_config, model_tool_name
from personal_agent.plan import PlanSession
from personal_agent.session import Session
from personal_agent.tool_registry import execute_tool, get_tool_definitions


SERVER_COMMAND = [sys.executable, "-X", "utf8", "-B", "-u", "-m", "examples.mcp_server_demo"]


def write_config(directory: Path, names=("demo",)) -> Path:
    path = directory / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {name: {
        "command": "{python}", "args": SERVER_COMMAND[1:], "cwd": str(PROJECT_ROOT), "timeout": 5,
    } for name in names}}), encoding="utf-8")
    return path


def call(call_id, name, arguments):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


class McpExecutionSmokeTests(unittest.TestCase):
    def test_real_call_distinguishes_tool_errors_from_rpc_errors(self):
        client = StdioMcpClient(SERVER_COMMAND)
        with client:
            client.discover()
            client.list_tools()
            result = client.call_tool("echo", {"text": "中文 MCP\n第二行"})
            self.assertFalse(result.is_error)
            self.assertEqual(result.content, ({"type": "text", "text": "中文 MCP\n第二行"},))
            self.assertEqual(result.structured_content, {"text": "中文 MCP\n第二行"})
            self.assertTrue(result.has_structured_content)
            invalid = client.call_tool("echo", {"text": 123})
            self.assertTrue(invalid.is_error)
            self.assertIn("string argument", invalid.as_text())
            with self.assertRaises(McpRemoteError) as raised:
                client.call_tool("missing", {})
            self.assertEqual(raised.exception.code, -32602)
            self.assertFalse(client.call_tool("echo", {"text": "错误后仍可调用"}).is_error)
            with patch.object(client, "_request", return_value={"content": [{"type": "text", "text": 123}]}):
                with self.assertRaises(McpProtocolError):
                    client.call_tool("echo", {})
            with patch.object(client, "_request", return_value={"content": [{"type": "image", "data": "DO_NOT_SEND_BASE64"}], "structuredContent": None}):
                rendered = client.call_tool("echo", {}).as_text()
                self.assertIn("未展示", rendered)
                self.assertIn("structuredContent: null", rendered)
                self.assertNotIn("DO_NOT_SEND_BASE64", rendered)
        self.assertEqual(client.returncode, 0)

    def test_two_servers_use_exact_names_and_permissions_before_any_call(self):
        original = get_tool_definitions()
        with tempfile.TemporaryDirectory() as temporary:
            config = write_config(Path(temporary), ("demo", "second"))
            with McpToolRuntime(load_mcp_config(config)) as runtime:
                self.assertEqual(runtime.names, {"mcp__demo__echo", "mcp__second__echo"})
                approve = Mock(return_value=True)
                with patch.object(runtime, "call", wraps=runtime.call) as actual:
                    name = "mcp__demo__echo"
                    denied = [
                        execute_tool(name, {"text": "no"}, mcp=runtime),
                        execute_tool(name, {"text": "no"}, mcp=runtime, confirm=Mock(return_value=False)),
                        execute_tool(name, {"text": "no"}, mcp=runtime, plan=PlanSession("plan"), allow=[name], confirm=approve),
                        execute_tool(name, {"text": "no"}, mcp=runtime, mode="bypassPermissions", deny=[name], confirm=approve),
                        execute_tool(name, {"text": "no"}, mcp=runtime, mode="dontAsk", confirm=approve),
                        execute_tool("mcp__unknown__echo", {}, mcp=runtime, mode="bypassPermissions"),
                    ]
                    self.assertTrue(all(item.startswith("工具调用失败：") for item in denied))
                    actual.assert_not_called()
                    approve.assert_not_called()
                    self.assertIn("确认通过", execute_tool(name, {"text": "确认通过"}, mcp=runtime, confirm=approve))
                    self.assertIn("第二服务", execute_tool("mcp__second__echo", {"text": "第二服务"}, mcp=runtime, allow=["mcp__second__echo"]))
                    self.assertEqual(actual.call_count, 2)
                # 外部 Schema 是副本；既不污染运行时，也不扩展全局内置注册表。
                definitions = runtime.definitions()
                definitions[0]["input_schema"]["properties"].clear()
                self.assertIn("text", runtime.definitions()[0]["input_schema"]["properties"])
            self.assertTrue(all(client.returncode == 0 for client in runtime.clients.values()))
            self.assertEqual(get_tool_definitions(), original)
            for raw in ("tool.with.dot", "x" * 128):
                name = model_tool_name("demo", raw)
                self.assertLessEqual(len(name), 64)
                self.assertRegex(name, re.compile(r"^[A-Za-z0-9_-]+$"))
            self.assertNotEqual(model_tool_name("demo", "tool.with.dot"), model_tool_name("demo", "tool_with_dot"))

    def test_repl_saves_paired_results_and_resume_does_not_replay(self):
        runtimes, observations, approvals = [], [], []
        owner_thread = threading.get_ident()

        def create_runtime(configs):
            runtime = McpToolRuntime(configs)
            runtimes.append(runtime)
            return runtime

        def approve(name, arguments, _):
            self.assertEqual(threading.get_ident(), owner_thread)
            approvals.append((name, arguments))
            return True

        def model(messages, definitions, *, on_usage):
            self.assertIn("mcp__demo__echo", {item["name"] for item in definitions})
            on_usage({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
            observations.append(messages.copy())
            if len(observations) == 1:
                return {"role": "assistant", "tool_calls": [
                    call("ok", "mcp__demo__echo", {"text": "实际外部结果"}),
                    call("bad", "mcp__demo__echo", {"text": 123}),
                ]}
            results = [message for message in messages if message["role"] == "tool"]
            self.assertEqual([item["tool_call_id"] for item in results], ["ok", "bad"])
            self.assertIn("实际外部结果", results[0]["content"])
            self.assertIn("工具调用失败：MCP 工具返回错误", results[1]["content"])
            return {"role": "assistant", "content": "已根据实际 MCP 结果回答。"}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = write_config(root)
            with patch("personal_agent.session.PROJECT_ROOT", root), \
                 patch("personal_agent.main.McpToolRuntime", side_effect=create_runtime), \
                 patch("personal_agent.main.discover_agents", return_value=([], [])), \
                 patch("personal_agent.main.discover_skills", return_value=([], [])), \
                 patch("personal_agent.main.get_model_message", side_effect=model), \
                 patch("personal_agent.main.confirm_in_terminal", side_effect=approve), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                with patch("builtins.input", side_effect=["/mcp", "请回显", "exit"]):
                    history = run_repl(session_name="mcp-flow", with_tools=True, mcp_config=config, mcp_eager=True)
                self.assertEqual(len(history), 5)
                self.assertEqual(len(approvals), 2)
                self.assertEqual(Session("mcp-flow").load(), history)
                with patch("builtins.input", side_effect=["继续回答，不调用工具", "exit"]):
                    restored = run_repl(session_name="mcp-flow", resume=True, mcp_config=config, mcp_eager=True)
                self.assertEqual(restored[:5], history)
                self.assertEqual(len(restored), 7)
                self.assertEqual(len(approvals), 2)
            self.assertEqual(len(observations), 3)
            self.assertEqual(len(runtimes), 2)
            self.assertTrue(all(client.returncode == 0 for runtime in runtimes for client in runtime.clients.values()))
            self.assertEqual(runtimes[0].clients["demo"].stderr_tail.count("tools/call"), 2)
            self.assertNotIn("tools/call", runtimes[1].clients["demo"].stderr_tail)


if __name__ == "__main__":
    unittest.main()
