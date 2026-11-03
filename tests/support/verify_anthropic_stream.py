"""真实 SDK/SSE 的本地流式演示；--live 另验证当前配置协议的实际只读流。"""

import argparse
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import httpx2
from anthropic import Anthropic

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.support.verify_dual_protocol import response
from personal_agent.config import get_model_settings, load_model_config
from personal_agent.llm import stream_model_message
from personal_agent.main import run_repl
from personal_agent.session import Session
from personal_agent.tool_registry import execute_tool
from personal_agent.usage import UsageStats


def stream_events(blocks, reason="end_turn", *, stop=True):
    """固定事件，故意交错多个工具的参数片段；供演示和两条核心测试复用。"""
    initial = response([], None)
    initial["usage"]["output_tokens"] = 1
    events = [{"type": "message_start", "message": initial}, {"type": "ping"}]
    deltas = []
    for index, block in enumerate(blocks):
        if block["type"] == "text":
            start = {"type": "text", "text": ""}
            text = block["text"]
            chunks = [{"type": "text_delta", "text": text[:len(text)//2]},
                      {"type": "text_delta", "text": text[len(text)//2:]}]
        elif block["type"] == "tool_use":
            start = {**block, "input": {}}
            raw = json.dumps(block["input"], ensure_ascii=False)
            chunks = [{"type": "input_json_delta", "partial_json": raw[:len(raw)//2]},
                      {"type": "input_json_delta", "partial_json": raw[len(raw)//2:]}]
        else:
            start, chunks = block, []
        events.append({"type": "content_block_start", "index": index, "content_block": start})
        deltas.append(chunks)
    for part in (0, 1):
        for index, chunks in enumerate(deltas):
            if chunks:
                events.append({"type": "content_block_delta", "index": index, "delta": chunks[part]})
    events.extend({"type": "content_block_stop", "index": index} for index in range(len(blocks)))
    events.extend([
        {"type": "message_delta", "delta": {"stop_reason": None}, "usage": {"output_tokens": 3}},
        {"type": "message_delta", "delta": {"stop_reason": reason, "stop_sequence": None}, "usage": {"output_tokens": 7}},
    ])
    if stop:
        events.append({"type": "message_stop"})
    return events


class EventBody(httpx2.SyncByteStream):
    def __init__(self, events, before=None):
        self.events, self.before, self.closed = events, before, False

    def __iter__(self):
        for event in self.events:
            if self.before is not None:
                self.before(event)
            if isinstance(event, BaseException):
                raise event
            yield (f"event: {event['type']}\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode("utf-8")

    def close(self):
        self.closed = True


def verify_offline():
    requests, bodies = [], []
    output, errors = io.StringIO(), io.StringIO()
    config = {"protocol": "anthropic", "model": "offline-anthropic", "base_url": "https://example.invalid", "api_key": "offline"}
    tool_blocks = [{"type": "tool_use", "id": f"read_{n}", "name": "read_file", "input": {"file_path": f"playground/{n}.txt"}} for n in (1, 2)]
    tool_blocks.append({"type": "tool_use", "id": "write_1", "name": "write_file", "input": {"file_path": "playground/result.txt", "content": "甲+乙"}})

    def before_first(event):
        assert dispatch.call_count == 0  # 即使块已结束、参数完整，也不能提前执行。
        assert not (root / "playground/result.txt").exists()
        if event["type"] == "message_stop":
            assert "先读取" in output.getvalue()  # 正文确实在整条消息结束前显示。

    def handle(request):
        body = json.loads(request.content)
        assert body["stream"] is True and body["thinking"] == {"type": "disabled"}
        requests.append(body)
        if len(requests) == 2:
            return httpx2.Response(503, headers={"retry-after": "0.001"}, json={"type": "error", "error": {"type": "overloaded_error", "message": "offline"}})
        if len(requests) == 1:
            events = stream_events([{"type": "text", "text": "先读取两个文件。"}, *tool_blocks], "tool_use")
            content = EventBody(events, before_first)
        else:
            content = EventBody(stream_events([{"type": "text", "text": "已读取并写入。"}]))
        bodies.append(content)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=content)

    def client(**options):
        return Anthropic(**options, http_client=httpx2.Client(transport=httpx2.MockTransport(handle)))

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        (root / "playground").mkdir()
        for n, value in ((1, "甲"), (2, "乙")):
            (root / f"playground/{n}.txt").write_text(value, encoding="utf-8")
        with (
            patch("personal_agent.main.get_model_settings", return_value={k: v for k, v in config.items() if k != "api_key"}),
            patch("personal_agent.llm.load_model_config", return_value=config),
            patch("personal_agent.anthropic_stream.Anthropic", side_effect=client),
            patch("personal_agent.tools.PROJECT_ROOT", root), patch("personal_agent.session.PROJECT_ROOT", root),
            patch("personal_agent.main.discover_agents", return_value=([], [])),
            patch("personal_agent.main.discover_skills", return_value=([], [])),
            patch("personal_agent.main.execute_tool", wraps=execute_tool) as dispatch,
            redirect_stdout(output), redirect_stderr(errors),
        ):
            with patch("builtins.input", side_effect=["读写练习文件", "y", "/usage", "exit"]):
                history = run_repl(session_name="anthropic-stream", with_tools=True, stream=True)
            assert not errors.getvalue(), errors.getvalue()
            assert dispatch.call_count == 3
            assert Session("anthropic-stream").load() == history
            assert (root / "playground/result.txt").read_text(encoding="utf-8") == "甲+乙"
            with patch("builtins.input", side_effect=["继续回答", "exit"]):
                restored = run_repl(session_name="anthropic-stream", resume=True, stream=True)
            assert restored[:len(history)] == history and dispatch.call_count == 3
        results = requests[1]["messages"][-1]["content"]
        assert [block["tool_use_id"] for block in results] == ["read_1", "read_2", "write_1"]
        assert len(requests) == 4 and requests[1] == requests[2]
        assert all(body.closed for body in bodies)
        assert "输入 60，输出 14，总计 74" in output.getvalue()
        return {"status": "passed", "validation": "real Anthropic SDK/SSE / fixed model events / real CLI, approved file tools and sessions",
                "http_requests": len(requests), "logical_calls": 3, "tool_dispatches": dispatch.call_count,
                "text_visible_before_stop": True, "tools_before_complete_message": 0,
                "retry_body_identical": True, "resume_replayed_tools": False, "all_response_bodies_closed": True,
                "first_task_usage": {"prompt_tokens": 60, "completion_tokens": 14, "total_tokens": 74},
                "history": history, "next_request_tool_results": results, "artifact": "甲+乙",
                "temporary_files_removed_after_return": True}


def verify_live():
    from personal_agent.agent import run_agent_loop
    from personal_agent.workspace import tool_workspace
    config, stats, fragments, trace = load_model_config(), UsageStats(), [], []
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        (root / "playground").mkdir()
        marker = "流式工具验证-S8"
        (root / "playground/stream-check.txt").write_text(marker, encoding="utf-8")
        messages = [{"role": "user", "content": "调用 read_file 读取 playground/stream-check.txt，只回复文件中的验证词。"}]
        def model(history, definitions):
            return stream_model_message(history, [tool for tool in definitions if tool["name"] == "read_file"], on_text=fragments.append, on_usage=stats.record)
        def execute(name, arguments):
            assert name == "read_file" and arguments["file_path"] == "playground/stream-check.txt"
            with tool_workspace(root):
                result = execute_tool(name, arguments, mode="dontAsk")
            trace.append({"name": name, "arguments": arguments, "result": result})
            return result
        with tool_workspace(root):
            answer = run_agent_loop(messages, model, max_turns=3, tool_executor=execute)
        assert trace and marker in answer and fragments
    return {"status": "passed", "validation": "real model stream / real read_file / isolated temporary file",
            "protocol": config["protocol"], "model": config["model"], "answer": answer,
            "text_fragments": fragments, "trace": trace, "usage": vars(stats), "cost": "unknown; prices not configured"}
