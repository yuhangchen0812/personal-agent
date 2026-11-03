"""两条主流程：本地检索与权限，以及流式聊天激活后实际调用 MCP。"""

import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from personal_agent.main import run_repl
from personal_agent.mcp_runtime import McpToolRuntime
from personal_agent.permissions import READ_TOOLS
from personal_agent.plan import PlanSession
from personal_agent.session import Session
from personal_agent.tool_registry import execute_tool, get_tool_definitions
from personal_agent.tool_search import DeferredToolCatalog
from tests.test_mcp_execution import call, write_config


class ToolSearchSmokeTests(unittest.TestCase):
    def test_local_catalog_ranks_activates_and_obeys_search_permissions(self):
        tools = [{
            "name": name, "description": description,
            "input_schema": {"type": "object", "properties": {"text": {"type": "string", "description": "SCHEMA_ONLY_MARKER"}}},
        } for name, description in [
            ("mcp__weather__forecast", "查询南京天气预报 weather forecast"),
            ("mcp__demo__echo", "回显文本 echo text"),
            ("mcp__files__read", "读取资料 read documents"),
        ]]
        original_registry = get_tool_definitions()
        catalog = DeferredToolCatalog(tools)
        self.assertEqual(catalog.definitions(), [])
        self.assertNotIn("tool_search", READ_TOOLS)  # 激活状态写入在主线程执行。
        callback = Mock(side_effect=catalog.search)
        self.assertIn("权限拒绝", execute_tool("tool_search", {"query": "echo"}, deny=["tool_search"], search_tools=callback))
        self.assertIn("小于等于 5", execute_tool("tool_search", {"query": "echo", "limit": 6}, search_tools=callback))
        callback.assert_not_called()
        self.assertIn("非空", execute_tool("tool_search", {"query": "  "}, search_tools=callback))
        self.assertEqual(catalog.active_names, set())
        local = execute_tool("tool_search", {"query": "天气", "limit": 1}, plan=PlanSession("plan"), search_tools=callback)
        self.assertEqual([tool["name"] for tool in json.loads(local)["tools"]], ["mcp__weather__forecast"])
        self.assertNotIn("SCHEMA_ONLY_MARKER", local)
        self.assertIn("SCHEMA_ONLY_MARKER", str(catalog.definitions()))
        self.assertEqual(catalog.find("ECHO", 1)[0]["name"], "mcp__demo__echo")
        self.assertEqual(catalog.find("mcp__files__read", 1)[0]["name"], "mcp__files__read")
        self.assertEqual(catalog.find("echo", excluded_names={"mcp__demo__echo"}), [])
        runtime = Mock()
        runtime.names = {"mcp__demo__echo"}
        approval = Mock(return_value=True)
        gated = execute_tool("mcp__demo__echo", {"text": "no"}, mcp=runtime, allow=["mcp__demo__echo"], exposed_mcp_tools=set(), confirm=approval)
        self.assertIn("尚未提供", gated)
        runtime.call.assert_not_called()
        approval.assert_not_called()
        before = catalog.active_names
        self.assertEqual(json.loads(catalog.search("zzzznomatch"))["tools"], [])
        self.assertEqual(catalog.active_names, before)
        catalog.search("回显")
        self.assertEqual(catalog.active_names, {"mcp__weather__forecast", "mcp__demo__echo"})
        copied = catalog.definitions()
        copied[0]["input_schema"]["properties"].clear()
        self.assertIn("text", catalog.definitions()[0]["input_schema"]["properties"])
        self.assertEqual(DeferredToolCatalog(tools).active_names, set())
        catalog.reset()
        self.assertEqual(catalog.definitions(), [])
        self.assertEqual(len(DeferredToolCatalog(tools, eager=True).definitions()), 3)
        self.assertEqual(get_tool_definitions(), original_registry)

    def test_streamed_search_updates_next_request_and_resumed_chat_starts_unloaded(self):
        runtimes, requests, approvals = [], [], []
        owner = threading.get_ident()

        def create_runtime(configs):
            runtime = McpToolRuntime(configs)
            runtimes.append(runtime)
            return runtime

        def approve(name, arguments, _):
            self.assertEqual(threading.get_ident(), owner)
            approvals.append((name, arguments))
            return True

        def model(messages, definitions, *, on_text, on_usage):
            on_usage({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
            requests.append(json.loads(json.dumps(definitions)))
            names = {tool["name"] for tool in definitions}
            self.assertIn("tool_search", names)
            index = len(requests)
            if index == 1:
                self.assertFalse(any(name.startswith("mcp__") for name in names))
                return {"role": "assistant", "tool_calls": [
                    call("before", "mcp__demo__echo", {"text": "不能提前执行"}),
                    call("find", "tool_search", {"query": "回显", "limit": 1}),
                    call("same_batch", "mcp__demo__echo", {"text": "同批也不能提前执行"}),
                ]}
            if index == 2:
                self.assertIn("mcp__demo__echo", names)
                self.assertNotIn("mcp__second__echo", names)
                results = [item for item in messages if item["role"] == "tool"]
                self.assertIn("尚未提供", results[0]["content"])
                self.assertIn("mcp__demo__echo", results[1]["content"])
                self.assertNotIn("mcp__second__echo", results[1]["content"])
                self.assertNotIn("input_schema", results[1]["content"])
                self.assertIn("尚未提供", results[2]["content"])
                self.assertEqual(approvals, [])
                return {"role": "assistant", "tool_calls": [call("actual", "mcp__demo__echo", {"text": "实际延迟调用结果"})]}
            if index == 3:
                self.assertIn("实际延迟调用结果", messages[-1]["content"])
                self.assertEqual(messages[-1]["tool_call_id"], "actual")
            else:
                self.assertEqual(index, 4)
                self.assertFalse(any(name.startswith("mcp__") for name in names))
                self.assertIn("actual", str(messages))  # 旧历史不会自动激活工具。
            on_text("完成")
            return {"role": "assistant", "content": "完成"}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = write_config(root, ("demo", "second"))
            with patch("personal_agent.session.PROJECT_ROOT", root), \
                 patch("personal_agent.main.McpToolRuntime", side_effect=create_runtime), \
                 patch("personal_agent.main.discover_agents", return_value=([], [])), \
                 patch("personal_agent.main.discover_skills", return_value=([], [])), \
                 patch("personal_agent.main.stream_model_message", side_effect=model), \
                 patch("personal_agent.main.confirm_in_terminal", side_effect=approve), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                with patch("builtins.input", side_effect=["/mcp", "调用回显", "exit"]):
                    history = run_repl(session_name="lazy", with_tools=True, stream=True, mcp_config=config, deny=["mcp__second__echo"])
                self.assertEqual(len(history), 8)
                self.assertEqual(len(approvals), 1)
                self.assertEqual(Session("lazy").load(), history)
                with patch("builtins.input", side_effect=["依据历史回答即可", "exit"]):
                    restored = run_repl(session_name="lazy", resume=True, stream=True, mcp_config=config)
                self.assertEqual(restored[:8], history)
            self.assertTrue(all(client.returncode == 0 for runtime in runtimes for client in runtime.clients.values()))
            self.assertEqual(runtimes[0].clients["demo"].stderr_tail.count("tools/call"), 1)
            self.assertNotIn("tools/call", runtimes[0].clients["second"].stderr_tail)
            self.assertTrue(all("tools/call" not in client.stderr_tail for client in runtimes[1].clients.values()))
            self.assertFalse(any(tool["name"].startswith("mcp__") for tool in requests[0]))


if __name__ == "__main__":
    unittest.main()
