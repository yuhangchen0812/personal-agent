"""使用合成思考/签名与真实 SDK，验证原样回传、实际工具和 v4 会话往返。"""

import argparse
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
from unittest.mock import patch

import httpx2
from anthropic import Anthropic

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.support.verify_dual_protocol import response
from tests.support.verify_anthropic_stream import EventBody
from personal_agent.config import get_model_settings
from personal_agent.anthropic_adapter import parse_message
from personal_agent.llm import get_model_message
from personal_agent.main import run_repl
from personal_agent.session import Session
from personal_agent.tool_registry import execute_tool
from personal_agent.usage import UsageStats


def thinking_events(blocks, reason):
    events = [{"type": "message_start", "message": response([], None)}]
    for index, block in enumerate(blocks):
        kind = block["type"]
        if kind == "thinking":
            start = {"type": kind, "thinking": "", "signature": ""}
            deltas = [{"type": "thinking_delta", "thinking": block["thinking"]}]
            signature = block["signature"]
            deltas += [{"type": "signature_delta", "signature": signature[:len(signature)//2]},
                       {"type": "signature_delta", "signature": signature[len(signature)//2:]}]
        elif kind == "text":
            start, deltas = {"type": kind, "text": ""}, [{"type": "text_delta", "text": block["text"]}]
        elif kind == "tool_use":
            start = {**block, "input": {}}
            raw = json.dumps(block["input"], ensure_ascii=False)
            deltas = [{"type": "input_json_delta", "partial_json": raw[:len(raw)//2]},
                      {"type": "input_json_delta", "partial_json": raw[len(raw)//2:]}]
        else:
            start, deltas = deepcopy(block), []
        events.append({"type": "content_block_start", "index": index, "content_block": start})
        events.extend({"type": "content_block_delta", "index": index, "delta": delta} for delta in deltas)
        events.append({"type": "content_block_stop", "index": index})
    events.extend([{"type": "message_delta", "delta": {"stop_reason": reason, "stop_sequence": None}, "usage": {"output_tokens": 7}},
                   {"type": "message_stop"}])
    return events


def verify_offline():
    requests, bodies, documents, histories = [], [], {}, {}
    tool_blocks = [
        {"type": "thinking", "thinking": "synthetic-only\n", "signature": "opaque-签名-A=="},
        {"type": "text", "text": "准备读取。"},
        {"type": "tool_use", "id": "read", "name": "read_file", "input": {"file_path": "playground/source.txt"}},
        {"type": "redacted_thinking", "data": "opaque-redacted-B=="},
        {"type": "text", "text": "等待结果。"},
        {"type": "tool_use", "id": "denied", "name": "write_file", "input": {"file_path": "playground/denied.txt", "content": "不可写"}},
    ]
    final_blocks = [{"type": "thinking", "thinking": "", "signature": "opaque-final-签名=="},
                    {"type": "redacted_thinking", "data": "opaque-final-redacted=="},
                    {"type": "text", "text": "已读取甲，写入被拒绝。"}]
    output, errors = io.StringIO(), io.StringIO()
    active = {}

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["thinking"] == active["thinking"] and body["max_tokens"] == active["max_tokens"]
        for item in body["messages"]:
            if item["role"] == "assistant":
                assert item["content"] in (tool_blocks, final_blocks)
        blocks = tool_blocks if len(requests) == 1 else final_blocks
        reason = "tool_use" if blocks is tool_blocks else "end_turn"
        if body["stream"]:
            content = EventBody(thinking_events(blocks, reason))
            bodies.append(content)
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=content)
        return httpx2.Response(200, json=response(blocks, reason))

    def client(**options):
        return Anthropic(**options, http_client=httpx2.Client(transport=httpx2.MockTransport(handle)))

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        (root / "playground").mkdir()
        (root / "playground/source.txt").write_text("甲", encoding="utf-8")
        config = {"protocol": "anthropic", "api_key": "offline", "model": "offline-thinking", "base_url": "https://example.invalid",
                  "thinking": {"type": "adaptive"}, "max_tokens": 2048}
        active.update(config)
        with (
            patch("personal_agent.main.get_model_settings", side_effect=lambda: {key: value for key, value in active.items() if key != "api_key"}),
            patch("personal_agent.llm.load_model_config", side_effect=lambda: dict(active)),
            patch("personal_agent.anthropic_stream.Anthropic", side_effect=client),
            patch("personal_agent.anthropic_adapter.Anthropic", side_effect=client),
            patch("personal_agent.session.PROJECT_ROOT", root), patch("personal_agent.tools.PROJECT_ROOT", root),
            patch("personal_agent.main.discover_agents", return_value=([], [])),
            patch("personal_agent.main.discover_skills", return_value=([], [])),
            patch("personal_agent.main.execute_tool", wraps=execute_tool) as dispatch,
            redirect_stdout(output), redirect_stderr(errors),
        ):
            with patch("builtins.input", side_effect=["读取材料", "exit"]):
                first = run_repl(session_name="thinking-tools", with_tools=True, stream=True, permission_mode="dontAsk")
            assert dispatch.call_count == 2, errors.getvalue()
            assert first[1]["anthropic"]["content"] == tool_blocks
            assert first[-1]["anthropic"]["content"] == final_blocks
            session = Session("thinking-tools")
            assert session.load() == first
            with patch("builtins.input", side_effect=["继续回答", "exit"]):
                restored = run_repl(session_name="thinking-tools", resume=True, permission_mode="dontAsk")
            assert restored[:len(first)] == first and dispatch.call_count == 2
            documents["tools"] = json.loads(session.path.read_text(encoding="utf-8"))
            histories["tools"] = restored
            active["thinking"] = {"type": "enabled", "budget_tokens": 1024}
            with patch("builtins.input", side_effect=["纯文本问题", "exit"]):
                text = run_repl(session_name="thinking-text")
            with patch("builtins.input", side_effect=["继续纯文本", "exit"]):
                continued = run_repl(session_name="thinking-text", resume=True, stream=True)
            assert continued[:len(text)] == text and continued[-1]["anthropic"]["content"] == final_blocks
            documents["text"] = json.loads(Session("thinking-text").path.read_text(encoding="utf-8"))
            histories["text"] = continued
        assert not (root / "playground/denied.txt").exists()
        assert "甲" in requests[1]["messages"][-1]["content"][0]["content"]
        assert "权限拒绝" in requests[1]["messages"][-1]["content"][1]["content"]
        assert len(requests) == 5 and all(body.closed for body in bodies)
        assert all(document["version"] == 4 for document in documents.values())
        assert documents["tools"]["with_tools"] is True and documents["text"]["with_tools"] is False
        script = (
            "import json,sys; from pathlib import Path; import personal_agent.session as s; "
            "s.PROJECT_ROOT=Path(sys.argv[1]); print(json.dumps(s.Session(sys.argv[2]).load(),ensure_ascii=False))"
        )
        for key, name in (("tools", "thinking-tools"), ("text", "thinking-text")):
            loaded = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", script, str(root), name],
                                    cwd=ROOT, check=True, capture_output=True, encoding="utf-8")
            assert json.loads(loaded.stdout) == histories[key]
        assert "synthetic-only" not in output.getvalue() and "opaque-" not in output.getvalue()
        assert not errors.getvalue(), errors.getvalue()
        return {"status": "passed", "validation": "real SDK / fixed HTTP and SSE / synthetic thinking and signatures / real files and sessions",
                "http_requests": len(requests), "tool_dispatches": dispatch.call_count,
                "source_blocks": tool_blocks, "final_blocks": final_blocks, "histories": histories,
                "session_versions": {name: doc["version"] for name, doc in documents.items()},
                "with_tools": {name: doc["with_tools"] for name, doc in documents.items()},
                "wire_echo_exact": True, "write_denied": True, "resume_replayed_tools": False,
                "all_response_bodies_closed": True, "thinking_printed": False,
                "fresh_process_session_loads": 2,
                "signatures": "synthetic opaque strings; no cryptographic verification or real provider signature tested",
                "temporary_files_removed_after_return": True}


def verify_live_openai():
    settings = get_model_settings()
    if settings["protocol"] != "openai":
        raise ValueError("该真实验证仅用于当前 OpenAI 兼容服务，不会把合成签名发送到真实 Anthropic 服务。")
    signed = parse_message({"role": "assistant", "stop_reason": "end_turn", "content":
                            [{"type": "thinking", "thinking": "synthetic-only", "signature": "opaque-fixture=="},
                             {"type": "text", "text": "已保存演示上下文。"}]},
                           tools_enabled=False, thinking={"type": "adaptive"}, model="offline-thinking")
    messages = [{"role": "user", "content": "保存演示上下文。"}, signed,
                {"role": "user", "content": "请只回复：跨协议继续验证-P9"}]
    before = deepcopy(messages)
    usage = UsageStats()
    result = get_model_message(messages, on_usage=usage.record, max_tokens=64)
    assert messages == before and "跨协议继续验证-P9" in result["content"]
    return {"status": "passed", "validation": "real OpenAI-compatible API / synthetic prior Anthropic history / provider metadata removed from request",
            "protocol": settings["protocol"], "model": settings["model"], "answer": result["content"],
            "usage": vars(usage), "original_history_unchanged": True, "cost": "unknown; prices not configured"}
