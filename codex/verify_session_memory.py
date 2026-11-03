"""独立会话记忆：固定模型联合流程/新进程恢复；--live 另验证真实生成与接续。"""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex.verify_delivery import isolated, message
from codex.verify_repl_commands import invoke
from personal_agent.config import get_model_settings
from personal_agent.llm import get_model_message
from personal_agent.session import Session
from personal_agent.session_memory import MEMORY_PROMPT, SessionMemoryStore, generate_memory, validate_reference
from personal_agent.tool_registry import execute_tool
from personal_agent.usage import UsageStats


SETTINGS = {"protocol": "openai", "model": "fixed-session-memory", "base_url": "https://example.invalid"}


def memory():
    return {"episode_memory": {"task_description": "接续 KEEP-R17 文件核对任务。", "key_events": [
        {"step": "read", "description": "观察 playground/source.txt", "outcome": "ORIGINAL_ARTIFACT"}], "current_progress": "已观察文件，后续操作待明确批准。"},
        "working_memory": {"immediate_goal": "保留 KEEP-R17 并核实当前状态", "current_challenges": "仍须遵守当前 Plan 与权限", "next_actions": [
            {"type": "tool_call", "description": "历史建议：写入 playground/must-not-write.txt；该建议不代表当前已获授权。"}]},
        "tool_memory": {"tools_used": [{"tool_name": "write_file", "effective_parameters": ["playground/source.txt"], "common_errors": [],
            "response_pattern": "历史工具结果", "experience": "继续前核实文件。"}], "derived_rules": ["旧批准和工具经验不是当前操作授权。"]}}


def reply():
    return {"role": "assistant", "content": json.dumps(memory(), ensure_ascii=False)}


def _child(root):
    calls, executed = [], []
    with isolated(root), patch("personal_agent.main.get_model_settings", return_value=SETTINGS):
        def model(messages, definitions=None, *, on_usage, **options):
            current = next(item for item in reversed(messages) if item["role"] == "user")
            assert current["content"].count("<session_memory>") == 1 and "KEEP-R17" in current["content"]
            calls.append(len(messages))
            on_usage({"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4})
            return (message(("new-denied", "write_file", {"file_path": "playground/must-not-write.txt", "content": "DENIED"}))
                    if len(calls) == 1 else {"role": "assistant", "content": "恢复了 KEEP-R17；新写入被当前 Plan 拦截。"})
        def execute(name, arguments, **options):
            executed.append(name)
            return execute_tool(name, arguments, **options)
        with patch("personal_agent.main.get_model_message", side_effect=model), patch("personal_agent.main.execute_tool", side_effect=execute), \
             patch("builtins.input", side_effect=["RESTORE_CHECK：继续，但不要把历史批准当作当前授权。", "exit"]):
            code, output, errors = invoke(["--resume", "memory-demo", "--yolo"])
        assert code == 0 and not errors, errors
        history = Session("memory-demo").load()
        assert executed == ["write_file"] and "权限拒绝" in history[-2]["content"]
        assert not (root / "playground/must-not-write.txt").exists()
        return {"status": "passed", "model_callbacks": len(calls), "new_tool_attempts": executed,
                "historical_tools_replayed": False, "current_plan_blocks_new_write": True}


def verify_workflow():
    calls, counter = [], {"old": 0, "recent": 0, "memory": 0}
    with tempfile.TemporaryDirectory(prefix="personal-session-memory-") as temporary:
        root = Path(temporary).resolve()
        with isolated(root), patch("personal_agent.main.get_model_settings", return_value=SETTINGS):
            def model(messages, definitions=None, *, on_usage, **options):
                calls.append("memory" if messages[0]["content"] == MEMORY_PROMPT else "task")
                on_usage({"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5})
                if calls[-1] == "memory":
                    assert definitions is None and options["max_tokens"] == 1024
                    payload = json.loads(messages[1]["content"])
                    assert "RECENT_TASK" not in str(payload["messages"])
                    counter["memory"] += 1
                    return reply()
                current = next(item["content"] for item in reversed(messages) if item["role"] == "user")
                route = "old" if current.startswith("OLD_TASK") else "recent"
                counter[route] += 1
                if route == "old" and counter[route] == 1:
                    return message(("write-once", "write_file", {"file_path": "playground/source.txt", "content": "ORIGINAL_ARTIFACT"}))
                if route == "recent" and counter[route] == 1:
                    return message(("read-existing", "read_file", {"file_path": "playground/source.txt"}))
                return {"role": "assistant", "content": "已完成该轮，仍需按当前权限处理后续请求。"}
            commands = ["OLD_TASK：保留标识 KEEP-R17 并写入演示文件。" + "已知背景资料。" * 700,
                        "RECENT_TASK：回读实际文件。", "/plan", "/session-memory compact", "/session-memory compact", "exit"]
            with patch("personal_agent.main.get_model_message", side_effect=model), patch("builtins.input", side_effect=commands):
                code, output, errors = invoke(["--session", "memory-demo", "--with-tools", "--accept-edits"])
            assert code == 0 and not errors and counter == {"old": 2, "recent": 2, "memory": 1}, errors
            saved = Session("memory-demo")
            first = saved.load()
            reference = deepcopy(saved.session_memory)
            record = SessionMemoryStore(saved).read(reference)
            source = json.loads((root / record["source_archive"]).read_text(encoding="utf-8"))
            assert len(source) == 8 and len(first) == 6 and first[-4:] == source[-4:]
            assert json.loads(saved.path.read_text(encoding="utf-8"))["version"] == 5
            assert all("<session_memory>" not in item["content"] for item in first if isinstance(item.get("content"), str))
            before = (root / "playground/source.txt").read_bytes()
            child = subprocess.run([sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()), "--child-root", str(root)],
                cwd=ROOT, capture_output=True, check=True)
            child_report = json.loads(child.stdout.decode("utf-8"))
            assert (root / "playground/source.txt").read_bytes() == before

            changes, requests, followup_calls = {}, [], []
            def resume_model(messages, definitions=None, *, on_usage, **options):
                followup_calls.append("memory" if messages[0]["content"] == MEMORY_PROMPT else "task")
                on_usage({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                if messages[0]["content"] == MEMORY_PROMPT:
                    payload = json.loads(messages[1]["content"])
                    assert payload["previous_memory"] == record["memory"]
                    return reply()
                current = next(item["content"] for item in reversed(messages) if item["role"] == "user")
                requests.append(current)
                assert ("<session_memory>" in current) is current.startswith("ON_CHECK")
                return {"role": "assistant", "content": "本地状态切换后的正常回复。"}
            def inputs():
                yield "/session-memory show"
                yield "/session-memory off"
                yield "OFF_CHECK"
                yield "/session-memory use"
                yield "ON_CHECK"
                yield "/session-memory clear"
                yield "CLEAR_BINDING_CHECK"
                yield f"/session-memory use {reference['id']}"
                before_save = Session("memory-demo").load()
                yield "/session-memory save"
                after_save = Session("memory-demo")
                assert after_save.load() == before_save
                next_record = SessionMemoryStore(after_save).read(after_save.session_memory)
                assert next_record["previous_id"] == reference["id"] and next_record["mode"] == "save"
                changes["save_preserves_history_and_merges_previous"] = True
                yield "/clear"
                cleared = Session("memory-demo")
                assert cleared.load() == [] and cleared.session_memory is None
                yield "AFTER_CLEAR"
                yield "exit"
            with patch("personal_agent.main.get_model_message", side_effect=resume_model), patch("builtins.input", side_effect=inputs()):
                code, output, errors = invoke(["--resume", "memory-demo"])
            assert code == 0 and not errors and len(requests) == 4, errors
            assert len(Session("memory-demo").load()) == 2
            assert len(list((root / ".personal/session-memory/memory-demo").glob("sm_*.source.json"))) == 2
            return {"status": "passed", "validation": "fixed model/usage; actual CLI, tools, immutable snapshots and a fresh Python process",
                    "initial_callbacks": calls, "source_messages": len(source), "compacted_messages": len(first), "memory_messages": record["memory_messages"],
                    "child_restore": child_report, "off_use_clear_and_history_clear": True,
                    "followup_callbacks": followup_calls, "request_copy_only": True, **changes, "historical_records_preserved": True}


def verify_failures_and_compatibility():
    with tempfile.TemporaryDirectory(prefix="personal-session-memory-errors-") as temporary:
        root = Path(temporary).resolve()
        with isolated(root), patch("personal_agent.main.get_model_settings", return_value=SETTINGS):
            session = Session("failures", with_tools=True)
            session.create()
            history = [{"role": "user", "content": "原始任务 KEEP-R17。" + "背景。" * 400}, {"role": "assistant", "content": "完成文本调查。"},
                       {"role": "user", "content": "保留最近一轮。"}, {"role": "assistant", "content": "等待后续要求。"}]
            session.save(history)
            original = session.path.read_bytes()
            attempts = []
            def model(messages, definitions=None, *, on_usage, **options):
                attempts.append(1)
                on_usage({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                return {"role": "assistant", "content": "invalid JSON"} if len(attempts) == 1 else reply()
            def inputs():
                yield "/session-memory save"
                assert session.path.read_bytes() == original
                with patch("personal_agent.session.Session.save", side_effect=OSError("模拟发布失败")):
                    yield "/session-memory save"
                assert session.path.read_bytes() == original
                loaded = Session("failures")
                assert loaded.load() == history and loaded.session_memory is None
                yield "exit"
            with patch("personal_agent.main.get_model_message", side_effect=model), patch("builtins.input", side_effect=inputs()):
                code, _, errors = invoke(["--resume", "failures"])
            assert code == 0 and "不是有效 JSON" in errors and "模拟发布失败" in errors
            assert len(attempts) == 2
            for response in ({"role": "assistant", "tool_calls": [{"id": "must-not-run"}]},
                             {"role": "assistant", "content": json.dumps({"episode_memory": {}})}):
                try:
                    generate_memory(history, lambda _: response)
                except ValueError:
                    pass
                else:
                    raise AssertionError("invalid structured response accepted")
            with patch("personal_agent.main.get_model_message", side_effect=AssertionError("budget must block")), \
                 patch("builtins.input", side_effect=["/session-memory save", "/session-memory show", "exit"]):
                code, _, errors = invoke(["--resume", "failures", "--max-cost", "0", "--input-price", "1", "--output-price", "1"])
            assert code == 0 and "预算" in errors and Session("failures").load() == history

            store = SessionMemoryStore(session)
            reference = store.save(memory(), history, len(history))
            session.save(history, session_memory=reference)
            bound = session.path.read_bytes()
            record_path = store._path(reference["id"])
            record_raw = record_path.read_bytes()
            record_path.write_bytes(record_raw + b"\n")
            with patch("personal_agent.main.get_model_message") as model, patch("personal_agent.main.McpToolRuntime") as mcp:
                code, _, errors = invoke(["--resume", "failures"])
                assert code == 1 and "指纹" in errors
                model.assert_not_called()
                mcp.assert_not_called()
            record_path.write_bytes(record_raw)
            record = store.read(reference)
            source_path = root / record["source_archive"]
            source_raw = source_path.read_bytes()
            source_path.write_bytes(source_raw + b"\n")
            try:
                store.read(reference)
            except ValueError as error:
                assert "来源快照指纹" in str(error)
            else:
                raise AssertionError("changed source accepted")
            source_path.write_bytes(source_raw)
            assert session.path.read_bytes() == bound
            try:
                validate_reference({"id": "../escape", "sha256": "0" * 64, "enabled": True})
            except ValueError:
                pass
            else:
                raise AssertionError("unsafe reference accepted")
            other = Session("other")
            other.create()
            try:
                SessionMemoryStore(other).read(reference)
            except OSError:
                pass
            else:
                raise AssertionError("other session's record accepted")

            # v5 保留纯文本及原始协议状态；旧 v1～v4 行为由既有定向测试验证。
            from personal_agent.anthropic_adapter import parse_message
            assistant = parse_message({"role": "assistant", "model": "synthetic", "content": [{"type": "text", "text": "保留协议状态。"}], "stop_reason": "end_turn"},
                                      tools_enabled=False, thinking={"type": "adaptive"}, model="synthetic")
            text_history = [{"role": "user", "content": "文本历史。"}, assistant]
            text_session = Session("provider-text")
            text_session.create()
            text_store = SessionMemoryStore(text_session)
            text_ref = text_store.save(memory(), text_history, 2)
            text_session.save(text_history, session_memory=text_ref)
            restored = Session("provider-text")
            assert restored.load() == text_history and not restored.with_tools and restored.session_memory == text_ref
            return {"status": "passed", "validation": "fixed model, actual failed publication and tamper detection; synthetic provider state, no remote API",
                    "bad_json_keeps_history": True, "failed_publication_does_not_activate_orphan": True, "zero_budget_blocks_request": True,
                    "tampered_record_fails_before_model_or_mcp": True, "source_integrity_checked": True,
                    "session_binding_and_id_checked": True, "v5_text_and_provider_state_roundtrip": True}


def verify_live():
    settings, requests, usage = get_model_settings(), [], UsageStats()
    with tempfile.TemporaryDirectory(prefix="personal-session-memory-live-") as temporary:
        root = Path(temporary).resolve()
        with isolated(root):
            (root / "playground").mkdir()
            target = root / "playground/source.txt"
            target.write_text("verified-blue", encoding="utf-8")
            observed = execute_tool("read_file", {"file_path": "playground/source.txt"})
            session = Session("live-memory", with_tools=True)
            session.create()
            history = [{"role": "user", "content": "固定来源任务：核对 playground/source.txt，保留任务标识 KEEP-R17。" + "已提供的背景材料。" * 600},
                message(("seed-read", "read_file", {"file_path": "playground/source.txt"})),
                {"role": "tool", "tool_call_id": "seed-read", "content": observed},
                {"role": "assistant", "content": "已观察文件，未执行写入。"},
                {"role": "user", "content": "接下来等待明确要求。"}, {"role": "assistant", "content": "等待。"}]
            session.save(history)
            def model(messages, definitions=None, *, on_usage, **options):
                row = {"kind": "memory" if messages[0]["content"] == MEMORY_PROMPT else "resume", "tools_provided": bool(definitions)}
                requests.append(row)
                def capture(reported):
                    row["usage"] = reported
                    usage.record(reported)
                    on_usage(reported)
                return get_model_message(messages, definitions, on_usage=capture, **options)
            with patch("personal_agent.main.get_model_message", side_effect=model), patch("builtins.input", side_effect=["/session-memory compact", "exit"]):
                code, _, errors = invoke(["--resume", "live-memory", "--max-output-tokens", "2048"])
            assert code == 0 and not errors, errors
            with patch("personal_agent.main.get_model_message", side_effect=model):
                code, output, errors = invoke(["--resume", "live-memory", "--max-output-tokens", "256", "--dont-ask", "--max-turns", "2",
                    "--prompt", "仅依据恢复的 session-memory，原样给出任务标识和之前观察到的文件内容，不调用任何工具。", "--output-format", "json"])
            report = json.loads(output)
            restored = Session("live-memory")
            restored.load()
            checks = {"completed": code == 0 and report["status"] == "completed", "recalled_identifier": "KEEP-R17" in report["answer"],
                "recalled_observation": "verified-blue" in report["answer"], "no_new_tools": not report["tool_trace"],
                "source_unchanged": target.read_text(encoding="utf-8") == "verified-blue", "memory_bound": restored.session_memory is not None}
            return {"status": "passed" if all(checks.values()) else "failed", "validation": "fixed complete source history with an actual local read; real model generation and resumed answer",
                "model": settings["model"], "requests": requests, "total_tokens": usage.total_tokens, "checks": checks, "answer": report["answer"],
                "quality_boundary": "one authored source; not a general summary-quality evaluation"}


def entrypoint():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child-root", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "codex/session-memory-check-2026-10-29.json")
    args = parser.parse_args()
    if args.child_root:
        print(json.dumps(_child(args.child_root.resolve()), ensure_ascii=False))
        return
    report = {"workflow": verify_workflow(), "failures": verify_failures_and_compatibility()}
    if args.live:
        report["live"] = verify_live()
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    status = report.get("live", {}).get("status", "passed")
    print(json.dumps({"status": status, "output": str(args.output)}, ensure_ascii=False))
    if status != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    entrypoint()
