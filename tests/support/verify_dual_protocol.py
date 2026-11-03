"""真实 SDK + 固定 HTTP 的协议演示；--live 另用当前配置做只读真实请求。"""

import argparse
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import httpx2
from anthropic import Anthropic

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from personal_agent.config import get_model_settings, load_model_config
from personal_agent.llm import get_model_message
from personal_agent.main import run_repl
from personal_agent.session import Session
from personal_agent.tool_registry import execute_tool
from personal_agent.usage import UsageStats


def response(content, reason="end_turn"):
    return {"id": "msg_offline", "type": "message", "role": "assistant", "model": "offline-anthropic",
            "content": content, "stop_reason": reason, "stop_sequence": None,
            "usage": {"input_tokens": 20, "output_tokens": 5,
                      "cache_creation_input_tokens": 3, "cache_read_input_tokens": 7}}


def verify_offline():
    requests, output, errors = [], io.StringIO(), io.StringIO()

    def handle(request):
        body = json.loads(request.content)
        assert request.url.path == "/v1/messages"
        assert (request.headers.get("x-api-key") == "offline"
                or request.headers.get("authorization") == "Bearer offline")
        requests.append(body)
        if len(requests) == 1:
            blocks = [{"type": "tool_use", "id": f"read_{n}", "name": "read_file",
                       "input": {"file_path": f"playground/{n}.txt"}} for n in (1, 2)]
            return httpx2.Response(200, json=response(blocks, "tool_use"))
        if len(requests) == 2:
            return httpx2.Response(503, headers={"retry-after": "0.001"},
                                   json={"type": "error", "error": {"type": "overloaded_error", "message": "offline retry"}})
        if len(requests) == 3:
            return httpx2.Response(200, json=response([{"type": "tool_use", "id": "blocked_write", "name": "write_file",
                "input": {"file_path": "playground/blocked.txt", "content": "应被拒绝"}}], "tool_use"))
        return httpx2.Response(200, json=response([{"type": "text", "text": "已读取甲和乙；写入被权限拒绝。"}]))

    def client(**options):
        return Anthropic(**options, http_client=httpx2.Client(transport=httpx2.MockTransport(handle)))

    config = {"protocol": "anthropic", "model": "offline-anthropic", "base_url": "https://example.invalid", "api_key": "offline"}
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        (root / "playground").mkdir()
        for n, text in ((1, "甲"), (2, "乙")):
            (root / f"playground/{n}.txt").write_text(text, encoding="utf-8")
        with (
            patch("personal_agent.main.get_model_settings", return_value={k: v for k, v in config.items() if k != "api_key"}),
            patch("personal_agent.llm.load_model_config", return_value=config),
            patch("personal_agent.anthropic_adapter.Anthropic", side_effect=client),
            patch("personal_agent.tools.PROJECT_ROOT", root),
            patch("personal_agent.session.PROJECT_ROOT", root),
            patch("personal_agent.main.discover_agents", return_value=([], [])),
            patch("personal_agent.main.discover_skills", return_value=([], [])),
            patch("personal_agent.main.execute_tool", wraps=execute_tool) as dispatch,
            redirect_stdout(output), redirect_stderr(errors),
        ):
            with patch("builtins.input", side_effect=["读取两个文件", "/usage", "exit"]):
                history = run_repl(session_name="protocol-demo", with_tools=True, permission_mode="dontAsk")
            assert Session("protocol-demo").load() == history
            assert dispatch.call_count == 3, (dispatch.call_count, errors.getvalue(), output.getvalue()[-500:])
            with patch("builtins.input", side_effect=["继续回答", "exit"]):
                restored = run_repl(session_name="protocol-demo", resume=True, permission_mode="dontAsk")
            assert restored[:len(history)] == history
            assert dispatch.call_count == 3  # 恢复不重放读取或写入。
        assert not (root / "playground/blocked.txt").exists()
        assert len(requests) == 5 and requests[1] == requests[2]
        results = requests[1]["messages"][-1]["content"]
        assert [block["tool_use_id"] for block in results] == ["read_1", "read_2"]
        assert "甲" in results[0]["content"] and "乙" in results[1]["content"]
        assert "权限拒绝" in requests[3]["messages"][-1]["content"][0]["content"]
        assert requests[0]["thinking"] == {"type": "disabled"}
        assert "enable_thinking" not in requests[0]
        assert "输入 90，输出 15，总计 105" in output.getvalue()
        assert not errors.getvalue(), errors.getvalue()
        return {"validation": "real SDK / mocked HTTP / real CLI, file reads and session files",
                "status": "passed", "http_requests": len(requests), "logical_calls": 4,
                "tool_dispatches": dispatch.call_count, "read_results": results,
                "retry_body_identical": True, "write_denied": True, "resume_replayed_tools": False,
                "history": history, "request_settings": {key: requests[0][key] for key in ("model", "max_tokens", "stream", "thinking")},
                "first_task_usage": {"prompt_tokens": 90, "completion_tokens": 15, "total_tokens": 105},
                "temporary_files_removed_after_return": True}


def verify_live():
    from personal_agent.agent import run_agent_loop
    from personal_agent.workspace import tool_workspace
    config = load_model_config()
    stats, observations = UsageStats(), []
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        (root / "playground").mkdir()
        marker = "双协议接入验证-P7"
        (root / "playground/protocol-check.txt").write_text(marker, encoding="utf-8")
        messages = [{"role": "user", "content": "调用 read_file 读取 playground/protocol-check.txt，只回复文件中的验证词。"}]
        def model(history, definitions):
            return get_model_message(history, [tool for tool in definitions if tool["name"] == "read_file"], on_usage=stats.record)
        def execute(name, arguments):
            assert name == "read_file" and arguments["file_path"] == "playground/protocol-check.txt"
            with tool_workspace(root):
                result = execute_tool(name, arguments, mode="dontAsk")
            observations.append({"name": name, "arguments": arguments, "result": result})
            return result
        with tool_workspace(root):
            answer = run_agent_loop(messages, model, tool_executor=execute, max_turns=3)
        assert observations and marker in answer
    return {"protocol": config["protocol"], "model": config["model"], "status": "passed",
            "validation": "real model API / real read_file / isolated temporary file",
            "answer": answer, "trace": observations, "usage": vars(stats), "cost": "unknown; prices not configured"}
