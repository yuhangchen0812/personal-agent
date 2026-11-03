"""千问 Messages 显式配置：固定 SDK/CLI 流程与可选一次性真实只读探针。"""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch
from uuid import uuid4

import httpx2
from anthropic import Anthropic

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex.verify_anthropic_thinking import thinking_events
from codex.verify_anthropic_stream import EventBody
from codex.verify_cli_config import invoke
from codex.verify_delivery import isolated
from codex.verify_dual_protocol import response
from codex.verify_qwen_thinking import reject
from personal_agent.agent import run_agent_loop
from personal_agent.anthropic_adapter import parse_message, prepare_messages
from personal_agent.anthropic_profile import QWEN_BASE_URL, state_identity
from personal_agent.config import get_model_settings, load_model_config, model_config_scope
from personal_agent.file_runtime import write_json_atomic
from personal_agent.llm import get_model_message, stream_model_message
from personal_agent.model_content import validate_anthropic_state, without_provider_state
from personal_agent.session import Session, validate_agent_messages
from personal_agent.source_snapshot import digest
from personal_agent.tool_registry import execute_tool
from personal_agent.usage import UsageStats
from personal_agent.workspace import tool_workspace


OPTIONS = {"protocol": "anthropic", "anthropic_provider": "qwen", "model": "qwen3.7-plus",
           "thinking": "enabled", "thinking_budget": 1024, "max_tokens": 2048}
REASON = "synthetic-gateway-field\n字段保真样本。\n"
BLOCKS = [{"type": "thinking", "thinking": REASON, "signature": ""},
          {"type": "tool_use", "id": "read", "name": "read_file", "input": {"file_path": "playground/input.txt"}},
          {"type": "tool_use", "id": "denied", "name": "write_file", "input": {"file_path": "playground/denied.txt", "content": "NO"}}]
FINAL = [{"type": "thinking", "thinking": REASON, "signature": ""}, {"type": "text", "text": "READ_OK"}]


def execute_probe_read(workspace, name, arguments):
    """仅限定工具与文件；合法的 offset/limit 等参数交给真实注册器校验。"""
    if name != "read_file" or not isinstance(arguments, dict) or arguments.get("file_path") != "playground/probe.txt":
        return "工具调用失败：只允许读取指定演示文件。"
    with tool_workspace(workspace):
        return execute_tool(name, arguments, mode="dontAsk")


def inspect_restore(root):
    with isolated(root), patch("personal_agent.tool_registry.execute_tool", side_effect=AssertionError("no replay")):
        for name in ("messages-json", "messages-stream"):
            history = Session(name).load()
            assert history[1]["anthropic"]["content"] == BLOCKS
            assert history[1]["anthropic"]["provider"] == "qwen"
    return {"status": "passed", "fresh_process_restore_no_tools": True}


def verify_workflow():
    requests = []
    with tempfile.TemporaryDirectory(prefix="qwen-Messages-中文 空格-") as temporary:
        root = Path(temporary).resolve()
        (root / "playground").mkdir()
        source = root / "playground/input.txt"
        source.write_text("ACTUAL_READ_EVIDENCE", encoding="utf-8")
        (root / "playground/probe.txt").write_text("BOUNDED_PROBE_READ", encoding="utf-8")
        assert "BOUNDED_PROBE_READ" in execute_probe_read(root, "read_file", {"file_path": "playground/probe.txt", "limit": 1})
        assert "工具调用失败" in execute_probe_read(root, "read_file", {"file_path": "playground/input.txt"})
        assert "工具调用失败" in execute_probe_read(root, "write_file", {"file_path": "playground/probe.txt", "content": "NO"})
        assert (root / "playground/probe.txt").read_text(encoding="utf-8") == "BOUNDED_PROBE_READ"
        with isolated(root), patch("personal_agent.config.ENV_FILE", root / "absent.env"), patch.dict(os.environ,
                {"DASHSCOPE_API_KEY": "qwen-fixture-private", "ANTHROPIC_API_KEY": "native-fixture-private"}, clear=True):
            def handle(request):
                body = json.loads(request.content)
                requests.append(body)
                assert str(request.url) == QWEN_BASE_URL + "/v1/messages"
                assert (request.headers.get("x-api-key") == "qwen-fixture-private"
                        or request.headers.get("authorization") == "Bearer qwen-fixture-private")
                assert body["thinking"] == {"type": "enabled", "budget_tokens": 1024} and body["max_tokens"] == 2048
                continuing = len(body["messages"]) > 1
                if continuing:
                    assert body["messages"][1]["content"] == BLOCKS
                    assert "ACTUAL_READ_EVIDENCE" in str(body["messages"][-1]) and "权限拒绝" in str(body["messages"][-1])
                blocks, reason = (FINAL, "end_turn") if continuing else (BLOCKS, "tool_use")
                if body["stream"]:
                    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=EventBody(thinking_events(blocks, reason)))
                return httpx2.Response(200, json=response(blocks, reason))

            def client(**options):
                return Anthropic(**options, http_client=httpx2.Client(transport=httpx2.MockTransport(handle)))

            with patch("personal_agent.anthropic_adapter.Anthropic", side_effect=client), patch("personal_agent.anthropic_stream.Anthropic", side_effect=client):
                for stream, name in ((False, "messages-json"), (True, "messages-stream")):
                    code, output, errors = invoke(["--protocol", "anthropic", "--anthropic-provider", "qwen", "--model", OPTIONS["model"],
                        "--thinking", "enabled", "--thinking-budget", "1024", "--max-output-tokens", "2048", "--session", name,
                        "--with-tools", "--dont-ask", "--deny-tool", "write_file", "--prompt", "读取实际文件；写入须被拒绝。", "--output-format", "json"] + (["--stream"] if stream else []))
                    assert code == 0, (code, errors)
                    result = json.loads(output)
                    assert result["answer"] == "READ_OK" and result["usage"]["calls"] == 2 and result["tool_error_count"] == 1
                    assert REASON not in output + errors
                    assert json.loads(Session(name).path.read_text(encoding="utf-8"))["version"] == 4
                    history = Session(name).load()
                    assert history[1]["anthropic"]["content"] == BLOCKS and history[-1]["anthropic"]["content"] == FINAL
            assert len(requests) == 4 and not (root / "playground/denied.txt").exists()
            assert source.read_text(encoding="utf-8") == "ACTUAL_READ_EVIDENCE"
            original = deepcopy(history)
            standard = without_provider_state(history)
            assert REASON not in str(standard) and history == original
            wire = prepare_messages(history + [{"role": "user", "content": "new task"}], None, "", thinking={"type": "adaptive"}, model="native")
            assert REASON not in str(wire) and history == original
            reject(lambda: prepare_messages(history[:-1], None, "", thinking={"type": "enabled", "budget_tokens": 1024}, model=OPTIONS["model"]))
            reject(lambda: parse_message(response(BLOCKS, "tool_use"), tools_enabled=True, thinking={"type": "enabled", "budget_tokens": 1024}, model=OPTIONS["model"]))
            invalid = deepcopy(history[1]); invalid["anthropic"]["base_url"] = "https://example.invalid/apps/anthropic"
            reject(lambda: validate_anthropic_state(invalid))
            invalid = deepcopy(history[1]); invalid["anthropic"]["content"][0]["signature"] = "unexpected"
            reject(lambda: validate_anthropic_state(invalid))
            for overrides in ({**OPTIONS, "base_url": "https://example.invalid/apps/anthropic"}, {**OPTIONS, "base_url": QWEN_BASE_URL + "/v1"},
                              {**OPTIONS, "model": "unknown"}, {**OPTIONS, "thinking": "adaptive", "thinking_budget": None},
                              {**OPTIONS, "thinking_budget": 2048}, {**OPTIONS, "protocol": "openai"}):
                reject(lambda: model_config_scope(overrides).__enter__())
            # 凭据来源由显式配置决定；native 不会自动回退到已有千问密钥。
            with model_config_scope({key: value for key, value in {**OPTIONS, "thinking": "disabled"}.items() if key != "thinking_budget"}):
                assert load_model_config()["api_key"] == "qwen-fixture-private"
            with model_config_scope({"protocol": "anthropic", "model": "native", "anthropic_provider": "native"}):
                assert load_model_config()["api_key"] == "native-fixture-private"
                os.environ.pop("ANTHROPIC_API_KEY")
                reject(load_model_config)
            # 完整错误流必须先失败，不能因为网关空签名规则而提前执行任何工具。
            bad = deepcopy(BLOCKS); bad[1]["input"] = {"x": float("nan")}
            with model_config_scope(OPTIONS):
                for events in (thinking_events(BLOCKS, "tool_use")[:-1], thinking_events(BLOCKS, "max_tokens"), thinking_events(bad, "tool_use")):
                    executed = []
                    def broken(**options):
                        return Anthropic(**options, http_client=httpx2.Client(transport=httpx2.MockTransport(
                            lambda request: httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=EventBody(events)))))
                    with patch("personal_agent.anthropic_stream.Anthropic", side_effect=broken):
                        reject(lambda: run_agent_loop([{"role": "user", "content": "bad stream"}], lambda messages, definitions:
                            stream_model_message(messages, definitions), tool_executor=lambda *args: executed.append(args)))
                    assert not executed
        process = subprocess.run([sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()), "--inspect-root", str(root)], cwd=root, capture_output=True, timeout=60)
        assert process.returncode == 0, process.stderr.decode("utf-8", errors="replace")
    return {"status": "passed", "validation": "real SDK with fixed HTTP/SSE; actual CLI/tools/v4/fresh process restore",
            "explicit_credential_source_endpoint_model_and_signature_profiles": True, "exact_raw_blocks_echo_body_separation": True,
            "cross_profile_request_copy_and_invalid_stream_guards": True, "new_remote_model_calls": 0}


def verify_live():
    """一次只读任务及一条流式短回复；失败停止，不切服务/型号重跑。"""
    folder = ROOT / ".personal/qwen-messages-live" / uuid4().hex
    workspace = folder / "workspace"
    (workspace / "playground").mkdir(parents=True, exist_ok=False)
    source = workspace / "playground/probe.txt"
    marker = "MESSAGES-LIVE-" + uuid4().hex[:12]
    source.write_text(marker + "\n", encoding="utf-8")
    original = source.read_bytes()
    stats, raw_usage, trace = UsageStats(), [], []
    history = [{"role": "user", "content": "先调用 read_file 读取 playground/probe.txt，然后只回复文件第一行标识，不能猜测或读取其他路径。"}]
    report = {"status": "running", "directory": str(folder), "checks": {}, "stream_status": "not_run"}
    try:
        with model_config_scope({**OPTIONS, "max_tokens": 1536}) as settings:
            report["settings"] = settings  # 非密钥作用域快照。
            def tracked(call):
                recorded = None
                def capture(value):
                    nonlocal recorded
                    recorded = value
                try:
                    return call(capture)
                finally:
                    stats.record(recorded); raw_usage.append(recorded)
            def model(messages, definitions):
                return tracked(lambda capture: get_model_message(messages, [item for item in definitions if item["name"] == "read_file"], on_usage=capture))
            def execute(name, arguments):
                result = execute_probe_read(workspace, name, arguments)
                trace.append({"name": name, "arguments": arguments, "result_sha256": digest(result.encode("utf-8")), "read_marker_observed": marker in result})
                return result
            answer = run_agent_loop(history, model, max_turns=3, tool_executor=execute)
            validate_agent_messages(history)
            report["checks"] = {"actual_tool_read": any(item["read_marker_observed"] for item in trace), "answer_contains_actual_marker": marker in answer,
                "gateway_reasoning_observed": any(any(block["type"] == "thinking" for block in item.get("anthropic", {}).get("content", [])) for item in history)}
            shown = []
            streamed = tracked(lambda capture: stream_model_message([{"role": "user", "content": "计算 13+29，只回复数字。"}], on_text=shown.append, on_usage=capture))
            report["stream_status"] = "completed"
            report["checks"].update(stream_body="".join(shown) == streamed["content"] and "42" in streamed["content"],
                stream_thinking_observed=any(block["type"] == "thinking" for block in streamed.get("anthropic", {}).get("content", [])),
                source_unchanged=source.read_bytes() == original, usage_reported_once=stats.calls == stats.reported_calls)
            write_json_atomic(folder / "stream-message.json", streamed)
            report["status"] = "passed" if all(report["checks"].values()) else "partial"
    except (Exception, KeyboardInterrupt) as error:
        report.update(status="cancelled" if isinstance(error, KeyboardInterrupt) else "failed", error_type=type(error).__name__)
        if getattr(error, "status_code", None) is not None:
            report["status_code"] = error.status_code
    write_json_atomic(folder / "history.json", history)
    write_json_atomic(folder / "tool-trace.json", trace)
    report.update(new_remote_model_logical_calls=stats.calls, usage=vars(stats), provider_usage=raw_usage, source_sha256=digest(original),
                  artifact_sha256={path.relative_to(folder).as_posix(): digest(path.read_bytes()) for path in folder.rglob("*") if path.is_file()},
                  boundary="one Qwen Anthropic-compatible read-only task and streaming arithmetic; empty gateway signatures are not native Anthropic signature acceptance; logical call counts are not physical attempts, SDK request retries remain configured")
    write_json_atomic(folder / "report.json", report)
    write_json_atomic(ROOT / "codex/qwen-messages-live-check-2026-11-01.json", report)
    return report


def check_live(folder):
    """独立读回文件、完整历史及用量；不发送请求或重放工具。"""
    report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "passed"
    for relative, expected in report["artifact_sha256"].items():
        assert digest((folder / relative).read_bytes()) == expected
    history = validate_agent_messages(json.loads((folder / "history.json").read_text(encoding="utf-8")))
    streamed = json.loads((folder / "stream-message.json").read_text(encoding="utf-8")); validate_anthropic_state(streamed)
    marker = (folder / "workspace/playground/probe.txt").read_text(encoding="utf-8").strip()
    assert marker in history[-1]["content"] and any(item["role"] == "tool" and marker in item["content"] for item in history)
    assert digest((folder / "workspace/playground/probe.txt").read_bytes()) == report["source_sha256"]
    assert sum(item["total_tokens"] for item in report["provider_usage"]) == report["usage"]["total_tokens"]
    result = {"status": "passed", "directory": str(folder), "report_sha256": digest((folder / "report.json").read_bytes()),
              "artifact_hashes_history_tool_result_and_usage_checked": True, "new_remote_model_calls": 0}
    write_json_atomic(ROOT / "codex/qwen-messages-evidence-check-2026-11-01.json", result)
    return result


if __name__ == "__main__":
    if sys.argv[1:2] == ["--inspect-root"]:
        result = inspect_restore(Path(sys.argv[2]))
    elif sys.argv[1:] == ["--live"]:
        result = verify_live()
    elif sys.argv[1:2] == ["--check-live"]:
        result = check_live(Path(sys.argv[2]))
    else:
        result = verify_workflow()
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if result["status"] == "passed" else 1)
