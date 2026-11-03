"""CLI 配置作用域：真实 SDK/固定 HTTP；--live 另做一次真实千问短请求。"""

import argparse
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import httpx2
from anthropic import Anthropic
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.support.verify_delivery import isolated, message
from tests.support.verify_dual_protocol import response
from personal_agent.config import get_model_settings, model_config_scope
from personal_agent.llm import get_model_message, stream_model_message
from personal_agent.main import main
from personal_agent.session import Session
from personal_agent.skill_eval import REPLAY_PROMPT, SkillEvaluator
from personal_agent.skill_evolution import DRAFT_PROMPT, SkillEvolutionStore
from personal_agent.skills import create_project_skill
from personal_agent.subagent import EXPLORE_SYSTEM_PROMPT


def completion(text=None, calls=None):
    assistant = {"role": "assistant", "content": text}
    if calls:
        assistant["tool_calls"] = calls
    return {"id": "cli-fixed", "object": "chat.completion", "created": 1, "model": "cli-openai",
            "choices": [{"index": 0, "message": assistant, "finish_reason": "tool_calls" if calls else "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}}


def invoke(arguments):
    output, errors = io.StringIO(), io.StringIO()
    code = 0
    with redirect_stdout(output), redirect_stderr(errors):
        try:
            main(arguments)
        except SystemExit as error:
            code = error.code
    return code, output.getvalue(), errors.getvalue()


def sdk_client(kind, handler):
    factory = {"openai": OpenAI, "anthropic": Anthropic}[kind]
    return lambda **options: factory(**options, http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))


def verify_inheritance():
    """实际 CLI → 父循环/子任务 → 真后台线程 → 原版/候选文本回放。"""
    requests, counters = [], {"parent": 0, "child": 0}
    main_thread = threading.get_ident()
    with tempfile.TemporaryDirectory(prefix="personal-cli-scope-") as temporary:
        root = Path(temporary).resolve()
        (root / "playground").mkdir()
        (root / "playground/input.txt").write_text("SCOPE_FILE_EVIDENCE", encoding="utf-8")
        env_file = root / ".env"
        env_file.write_text("PERSONAL_AGENT_MODEL=file-model\nPERSONAL_AGENT_PROTOCOL=openai\n", encoding="utf-8")
        env_bytes = env_file.read_bytes()
        environment = {"PERSONAL_AGENT_PROTOCOL": "anthropic", "PERSONAL_AGENT_MODEL": "env-model",
                       "PERSONAL_AGENT_BASE_URL": "https://env.invalid/v1", "PERSONAL_AGENT_MAX_TOKENS": "bad",
                       "ANTHROPIC_THINKING": "invalid-unused-protocol", "DASHSCOPE_API_KEY": "fixed-openai-key"}
        with isolated(root), patch("personal_agent.config.ENV_FILE", env_file), patch.dict(os.environ, environment, clear=True):
            skill = create_project_skill("explain-flow", "解释流程", "BASE_METHOD $ARGUMENTS")
            original = skill.read_bytes()
            store = SkillEvolutionStore()
            suite = {"version": 1, "name": "scope-development", "split": "development", "skill_name": "explain-flow",
                     "cases": [{"id": "scope", "prompt": "EVAL_SCOPE_TOKEN：说明另一个函数。", "rules": {"contains": ["EVAL_OK"]}}]}
            (root / "suite.json").write_text(json.dumps(suite, ensure_ascii=False), encoding="utf-8")

            def handle(request):
                body = json.loads(request.content)
                assert str(request.url) == "https://cli.invalid/gateway/chat/completions"
                assert request.headers["authorization"] == "Bearer fixed-openai-key"
                assert body["model"] == "cli-openai" and body["max_tokens"] == 1536 and body["enable_thinking"] is False
                contents = [item.get("content") or "" for item in body["messages"]]
                if DRAFT_PROMPT in contents:
                    route = "background"
                    assert threading.get_ident() != main_thread and "tools" not in body
                    result = completion(json.dumps({"lesson": "补充参数和返回值，列出有依据的调用顺序。", "rationale": "回应显式反馈"}, ensure_ascii=False))
                elif any(item.startswith(REPLAY_PROMPT) for item in contents):
                    route = "evaluation"
                    assert "TARGET_SCOPE_TASK" not in str(contents) and "tools" not in body
                    result = completion("EVAL_OK")
                elif EXPLORE_SYSTEM_PROMPT in contents:
                    route = "child"
                    n = counters[route]
                    counters[route] += 1
                    result = (completion(calls=message(("child-read", "read_file", {"file_path": "playground/input.txt"}))["tool_calls"])
                              if n == 0 else completion("已读取 SCOPE_FILE_EVIDENCE。"))
                else:
                    route = "parent"
                    n = counters[route]
                    counters[route] += 1
                    result = (completion(calls=message(("delegate", "agent", {"type": "explore", "prompt": "读取 playground/input.txt 并报告内容。"}))["tool_calls"])
                              if n == 0 else completion("TARGET_SCOPE_TASK 已完成。"))
                requests.append({"route": route, "model": body["model"], "max_tokens": body["max_tokens"], "url": str(request.url)})
                return httpx2.Response(200, json=result)

            def inputs():
                yield "TARGET_SCOPE_TASK：委派只读检查。"
                yield "/skill-learn explain-flow"
                yield "缺少返回值说明，请补充。"
                yield "/skill-jobs wait"
                candidate = store.list_records("candidate")[0]
                yield f"/skill-eval {candidate['id']} suite.json"
                yield "exit"

            with patch("personal_agent.llm.OpenAI", side_effect=sdk_client("openai", handle)), patch("builtins.input", side_effect=inputs()):
                code, output, errors = invoke(["--protocol", "openai", "--model", "cli-openai", "--api-base", "https://cli.invalid/gateway",
                    "--max-output-tokens", "1536", "--dont-ask", "--session", "scoped", "--with-tools"])
            assert code == 0 and not errors, errors
            assert counters == {"parent": 2, "child": 2}
            assert [row["route"] for row in requests].count("background") == 1
            assert [row["route"] for row in requests].count("evaluation") == 2
            evaluation = SkillEvaluator().list_reports()[0]
            candidate = store.list_records("candidate")[0]
            assert evaluation["status"] == "complete" and evaluation["model_settings"]["max_tokens"] == 1536
            assert candidate["status"] == "pending" and skill.read_bytes() == original
            history = Session("scoped").load()
            assert any(item["role"] == "tool" and "SCOPE_FILE_EVIDENCE" in item["content"] for item in history)
            assert "EVAL_SCOPE_TOKEN" not in str(history)
            assert dict(os.environ) == environment and env_file.read_bytes() == env_bytes
            # 退出作用域后恢复原协议；另一个作用域返回副本且可嵌套恢复。
            with model_config_scope({"protocol": "openai", "max_tokens": 256}):
                settings = get_model_settings()
                assert settings["model"] == "env-model"
                settings["model"] = "mutated-copy"
                with model_config_scope({"protocol": "openai", "model": "nested", "max_tokens": 32}):
                    assert get_model_settings()["model"] == "nested"
                assert get_model_settings()["model"] == "env-model"
            assert os.environ["PERSONAL_AGENT_PROTOCOL"] == "anthropic"
            # 无环境模型时才读取 .env；仍不改写源文件。
            with patch.dict(os.environ, {}, clear=True):
                assert get_model_settings()["model"] == "file-model"
            assert env_file.read_bytes() == env_bytes
            return {"status": "passed", "validation": "actual CLI and SDK, fixed HTTP, real child file read/background thread/session/evaluation",
                    "requests": requests, "background_inherits_scope": True, "environment_and_dotenv_unchanged": True,
                    "candidate_pending": True, "evaluation_decision": evaluation["decision"], "nested_scope_restored": True}


def sse(events):
    return "".join(f"event: {item['type']}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n" for item in events).encode("utf-8")


def verify_protocol_and_permissions():
    requests, permissions = [], []
    with tempfile.TemporaryDirectory(prefix="personal-cli-flags-") as temporary:
        root = Path(temporary).resolve()
        (root / "playground").mkdir()
        environment = {"PERSONAL_AGENT_PROTOCOL": "openai", "PERSONAL_AGENT_MODEL": "env-openai",
                       "ANTHROPIC_THINKING": "enabled", "ANTHROPIC_THINKING_BUDGET": "bad-stale-budget",
                       "ANTHROPIC_MAX_TOKENS": "bad-stale-limit", "ANTHROPIC_API_KEY": "fixed-anthropic-key",
                       "DASHSCOPE_API_KEY": "fixed-openai-key"}
        with isolated(root), patch("personal_agent.config.ENV_FILE", root / "missing.env"), patch.dict(os.environ, environment, clear=True):
            def anthropic_handle(request):
                body = json.loads(request.content)
                assert str(request.url) == "https://anthropic-cli.invalid/gateway/v1/messages"
                assert (request.headers.get("x-api-key") == "fixed-anthropic-key"
                        or request.headers.get("authorization") == "Bearer fixed-anthropic-key")
                assert body["model"] == "cli-anthropic" and body["max_tokens"] == 2048
                requests.append({"protocol": "anthropic", "stream": body.get("stream", False), "thinking": body["thinking"], "max_tokens": body["max_tokens"]})
                reply = response([{"type": "text", "text": "CLI_ANTHROPIC_OK"}])
                reply["model"] = body["model"]
                if not body.get("stream"):
                    return httpx2.Response(200, json=reply)
                start = {**reply, "content": [], "stop_reason": None}
                events = [{"type": "message_start", "message": start},
                    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "CLI_ANTHROPIC_OK"}},
                    {"type": "content_block_stop", "index": 0},
                    {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 5}},
                    {"type": "message_stop"}]
                return httpx2.Response(200, content=sse(events), headers={"content-type": "text/event-stream"})

            base = ["--protocol", "anthropic", "--model", "cli-anthropic", "--api-base", "https://anthropic-cli.invalid/gateway",
                    "--max-output-tokens", "2048", "--prompt", "只回复确认。", "--output-format", "json"]
            with patch("personal_agent.anthropic_adapter.Anthropic", side_effect=sdk_client("anthropic", anthropic_handle)), \
                 patch("personal_agent.anthropic_stream.Anthropic", side_effect=sdk_client("anthropic", anthropic_handle)):
                for extra, expected in [(["--thinking", "enabled", "--thinking-budget", "1024"], {"type": "enabled", "budget_tokens": 1024}),
                                        (["--thinking", "--stream"], {"type": "adaptive"}),
                                        (["--thinking", "disabled"], {"type": "disabled"})]:
                    code, output, errors = invoke(base + extra)
                    report = json.loads(output)
                    assert code == 0 and report["status"] == "completed", errors
                    assert report["answer"] == "CLI_ANTHROPIC_OK"
                    assert report["model"]["thinking"] == expected and report["model"]["base_url"] == "https://anthropic-cli.invalid/gateway"
                    assert requests[-1]["thinking"] == expected
            assert get_model_settings()["model"] == "env-openai"  # protocol 和缺省模型均恢复。

            def openai_handle(request):
                body = json.loads(request.content)
                assert body["model"] == "env-openai" and body["max_tokens"] == 77
                assert str(request.url) == "https://openai-cli.invalid/v1/chat/completions"
                requests.append({"protocol": "openai", "stream": body.get("stream", False), "max_tokens": body["max_tokens"]})
                if body.get("stream"):
                    chunk = {"id": "scope-stream", "object": "chat.completion.chunk", "created": 1, "model": "env-openai",
                        "choices": [{"index": 0, "delta": {"role": "assistant", "content": "STREAM_OK"}, "finish_reason": None}]}
                    finish = {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                    usage = {**chunk, "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}}
                    content = "".join(f"data: {json.dumps(item)}\n\n" for item in (chunk, finish, usage)) + "data: [DONE]\n\n"
                    return httpx2.Response(200, content=content, headers={"content-type": "text/event-stream"})
                if body["messages"][-1]["role"] != "tool":
                    return httpx2.Response(200, json=completion(calls=message(("write", "write_file", {"file_path": "playground/out.txt", "content": "PERMISSION_OK"}))["tool_calls"]))
                return httpx2.Response(200, json=completion("已根据工具结果结束。"))

            with patch("personal_agent.llm.OpenAI", side_effect=sdk_client("openai", openai_handle)):
                for flag, extra, written in [("--accept-edits", [], True), ("--dont-ask", [], False),
                                              ("--plan", ["--allow-tool", "write_file"], False),
                                              ("--yolo", ["--deny-tool", "write_file"], False)]:
                    target = root / "playground/out.txt"
                    if target.exists():
                        target.unlink()
                    code, output, errors = invoke(["--api-base", "https://openai-cli.invalid/v1", "--max-output-tokens", "77",
                        flag, *extra, "--prompt", "执行写入测试。", "--output-format", "json"])
                    report = json.loads(output)
                    assert code == 0 and report["status"] == "completed", errors
                    assert target.exists() is written
                    if written:
                        assert target.read_text(encoding="utf-8") == "PERMISSION_OK"
                    permissions.append({"flag": flag, "written": written, "trace_outcome": report["tool_trace"][0]["outcome"]})
                code, output, errors = invoke(["--api-base", "https://openai-cli.invalid/v1", "--max-output-tokens", "77",
                    "--thinking", "disabled", "--stream", "--prompt", "短回复。", "--output-format", "json"])
                assert code == 0 and json.loads(output)["answer"] == "STREAM_OK", errors

            # 参数/配置失败均发生在会话、MCP 及模型创建之前；下一次调用恢复环境设置。
            invalid = [(["--max-output-tokens", "0"], 2), (["--thinking-budget", "0"], 2),
                       (["--plan", "--yolo"], 2), (["--permission-mode", "default", "--dont-ask"], 2),
                       (["--model", ""], 1), (["--api-base", "https://user:password@example.invalid"], 1),
                       (["--thinking", "enabled"], 1),
                       (["--protocol", "anthropic", "--model", "test", "--thinking", "enabled", "--thinking-budget", "2048", "--max-output-tokens", "2048"], 1)]
            with patch("personal_agent.main.Session") as session, patch("personal_agent.main.McpToolRuntime") as mcp, patch("personal_agent.llm.OpenAI") as client:
                for flags, expected_code in invalid:
                    code, _, errors = invoke(["--session", "must-not-create", *flags])
                    assert code == expected_code, (flags, code, errors)
                    assert get_model_settings()["model"] == "env-openai"
                session.assert_not_called()
                mcp.assert_not_called()
                client.assert_not_called()
            try:
                with model_config_scope({"protocol": "openai", "model": "exception-only"}):
                    raise RuntimeError("scope-exit")
            except RuntimeError:
                pass
            assert get_model_settings()["model"] == "env-openai" and dict(os.environ) == environment
            return {"status": "passed", "validation": "actual CLI and both SDKs, fixed HTTP/SSE, real permission-controlled writes",
                    "requests": requests, "permissions": permissions, "invalid_inputs_before_resources": len(invalid),
                    "protocol_key_selected": True, "scope_restored_after_error": True,
                    "remote_anthropic": "not_run; no configured remote credentials/model"}


def verify_live():
    settings = get_model_settings()
    if settings["protocol"] != "openai":
        return {"status": "skipped", "reason": "current protocol is not openai; no endpoint guessing"}
    with tempfile.TemporaryDirectory(prefix="personal-cli-live-") as temporary, isolated(Path(temporary).resolve()):
        code, output, errors = invoke(["--protocol", "openai", "--model", settings["model"], "--api-base", settings["base_url"],
            "--max-output-tokens", "64", "--dont-ask", "--max-turns", "2", "--prompt", "不要调用工具，只原样回复 CLI_SCOPE_OK。", "--output-format", "json"])
        report = json.loads(output)
        assert code == 0 and report["status"] == "completed" and report["answer"] == "CLI_SCOPE_OK", errors
        assert report["model"]["max_tokens"] == 64
        return {"status": "passed", "validation": "real current OpenAI-compatible API via actual CLI flags", "runtime": report}
