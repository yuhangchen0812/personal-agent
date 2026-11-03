"""验证检索 -> 动态 Schema -> 实际 MCP 调用；默认固定模型，--live 使用真实模型。"""

import argparse
import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from personal_agent.config import load_model_config
from personal_agent.llm import get_model_message
from personal_agent.main import run_repl
from personal_agent.mcp_runtime import McpToolRuntime
from personal_agent.permissions import SUPPORTED_TOOLS
from personal_agent.session import Session


def json_chars(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def run_check(*, live=False):
    config = load_model_config() if live else None
    expected = "松果-延迟工具-20261028"
    prompt = f"请先调用 tool_search，query=echo、limit=1，取得下一次请求里的参数 Schema 后，仅调用一次找到的 mcp__demo__echo，参数 text 精确为 {expected}。最后根据真实结果简短回复该文本。"
    session_name = "mcp_search_" + uuid4().hex[:8]
    runtimes, requests, approvals = [], [], []
    stdout, stderr = io.StringIO(), io.StringIO()

    def create_runtime(configs):
        runtime = McpToolRuntime(configs)
        runtimes.append(runtime)
        return runtime

    def approve(name, arguments, reason):
        accepted = not approvals and name == "mcp__demo__echo" and arguments == {"text": expected}
        approvals.append({"name": name, "arguments": arguments, "accepted": accepted})
        return accepted

    def tool_call(call_id, name, arguments):
        return {"role": "assistant", "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]}

    def model(messages, definitions, *, on_usage):
        names = {tool["name"] for tool in definitions}
        all_definitions = [*definitions, *(tool for tool in runtimes[0].definitions() if tool["name"] not in names)]
        observation = {
            "messages_count": len(messages), "mcp_tools": sorted(name for name in names if name.startswith("mcp__")),
            "tool_definitions_json_chars": json_chars(definitions),
            "all_tools_json_chars_same_request": json_chars(all_definitions),
        }
        requests.append(observation)

        def capture(usage):
            observation["usage"] = usage
            on_usage(usage)

        if live:
            return get_model_message(messages, definitions, on_usage=capture)
        capture({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
        if len(requests) == 1:
            return tool_call("search", "tool_search", {"query": "echo", "limit": 1})
        if len(requests) == 2:
            return tool_call("echo", "mcp__demo__echo", {"text": expected})
        return {"role": "assistant", "content": expected}

    with patch("personal_agent.main.McpToolRuntime", side_effect=create_runtime), \
         patch("personal_agent.main.get_model_message", side_effect=model), \
         patch("personal_agent.main.confirm_in_terminal", side_effect=approve), \
         patch("builtins.input", side_effect=[prompt, "exit"]), \
         redirect_stdout(stdout), redirect_stderr(stderr):
        history = run_repl(max_turns=4, deny=sorted(SUPPORTED_TOOLS - {"tool_search"}), mcp_config=ROOT / "examples/mcp-config.json", session_name=session_name, with_tools=True)
        saved = Session(session_name).load()
        with patch("builtins.input", side_effect=["/mcp", "exit"]):
            restored = run_repl(mcp_config=ROOT / "examples/mcp-config.json", session_name=session_name, resume=True)
    results = [message for message in history if message["role"] == "tool"]
    calls = [call["function"]["name"] for message in history for call in message.get("tool_calls", [])]
    clients = [client for runtime in runtimes for client in runtime.clients.values()]
    success = (
        len(requests) == 3 and requests[0]["mcp_tools"] == []
        and requests[1]["mcp_tools"] == requests[2]["mcp_tools"] == ["mcp__demo__echo"]
        and calls == ["tool_search", "mcp__demo__echo"]
        and "input_schema" not in results[0]["content"] and expected in results[1]["content"]
        and not results[1]["content"].startswith("工具调用失败：")
        and len(approvals) == 1 and approvals[0]["accepted"]
        and history[-1]["role"] == "assistant" and expected in history[-1]["content"]
        and saved == history == restored and len(runtimes) == 2
        and "tools/call" not in runtimes[1].clients["demo"].stderr_tail
        and all(client.returncode == 0 for client in clients)
    )
    return {
        "validation": "live configured model" if live else "fixed model and fixed usage; no model network request",
        "actual_components": "run_repl, stdio discovery/list/call, permission checks, JSON session save/restore",
        "api_key_present": bool(config["api_key"]) if config else None, "model": config["model"] if config else "scripted",
        "requests": requests, "approvals": approvals, "history": history,
        "first_request_omitted_definition_chars": requests[0]["all_tools_json_chars_same_request"] - requests[0]["tool_definitions_json_chars"] if requests else None,
        "measurement": "Python characters in compact JSON of internal definitions; not API wire bytes, tokens, latency or total-task cost",
        "session_name": session_name, "saved_history_matches": saved == history, "restored_history_matches": restored == history,
        "servers": [{"pid": client.pid, "returncode_after_close": client.returncode, "stderr": client.stderr_tail} for client in clients],
        "stdout": stdout.getvalue(), "stderr": stderr.getvalue(), "success": success,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="使用当前配置模型，会产生实际模型请求")
    args = parser.parse_args()
    report = run_check(live=args.live)
    label = "live-" if args.live else ""
    path = ROOT / f"codex/mcp-search-{label}check-2026-10-28.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"report": str(path), "model": report["model"], "model_requests": len(report["requests"]), "omitted_definition_chars": report["first_request_omitted_definition_chars"], "success": report["success"]}, ensure_ascii=False))
    if not report["success"]:
        raise SystemExit(1)
