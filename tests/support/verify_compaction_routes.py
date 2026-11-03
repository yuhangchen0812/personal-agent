"""统一折叠入口：默认固定模型验证；--live 额外使用当前真实模型。"""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.support.verify_delivery import isolated, message, trace
from tests.support.verify_repl_commands import invoke
from tests.support.verify_session_memory import memory
from personal_agent.config import get_model_settings
from personal_agent.context import SUMMARY_ACK, SUMMARY_PREFIX, SUMMARY_PROMPT, context_chars
from personal_agent.llm import get_model_message
from personal_agent.plan import PlanSession
from personal_agent.session import Session
from personal_agent.session_memory import MEMORY_PROMPT, SessionMemoryStore, with_session_memory
from personal_agent.tool_registry import execute_tool
from personal_agent.usage import UsageStats


SETTINGS = {"protocol": "openai", "model": "fixed-compaction-routes", "base_url": "https://example.invalid"}
NAME = "compact-routes"


def memory_value():
    value = memory()
    value["episode_memory"]["task_description"] = "接续 ROUTE-R11，只读核对 playground/source.txt。"
    value["episode_memory"]["key_events"][0]["outcome"] = "verified-jade"
    return value


def seed(session, *, legacy=False):
    """旧 v1/v3 来源；文件观察是真实本地读取，任务历史是预置资料。"""
    target = session.project_root / "playground/source.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("verified-jade", encoding="utf-8")
    history = [
        {"role": "user", "content": "任务 ROUTE-R11：只读核对 playground/source.txt。" + "背景资料，无新增操作要求。" * 400},
        {"role": "assistant", "content": "已观察实际文件内容 verified-jade。"},
        {"role": "user", "content": "保留本轮，等待继续。" + "当前轮次资料。" * 200},
        {"role": "assistant", "content": "等待下一条明确要求。"},
    ]
    if session.with_tools:
        history[1:2] = [message(("historical-read", "read_file", {"file_path": "playground/source.txt"})),
            {"role": "tool", "tool_call_id": "historical-read", "content": execute_tool("read_file", {"file_path": "playground/source.txt"})},
            history[1]]
    if legacy:
        history[:2] = [{"role": "user", "content": SUMMARY_PREFIX + '\n{"goal":"LEGACY-R11"}'},
            {"role": "assistant", "content": SUMMARY_ACK}]
        history[-2]["content"] += "迁移来源资料。" * 200
    session.create()
    session.save(history, plan=PlanSession("plan").snapshot() if session.with_tools else None)
    return history


def verify_routes():
    requests, summaries, records, counters, executed, sizes = [], [], [], {}, [], []
    with tempfile.TemporaryDirectory(prefix="personal-compact-routes-") as temporary:
        root = Path(temporary).resolve()
        with isolated(root), patch("personal_agent.main.get_model_settings", return_value=SETTINGS):
            session = Session(NAME, with_tools=True)
            original = seed(session)
            def model(messages, definitions=None, *, on_usage, **options):
                on_usage({"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3})
                if messages[0]["content"] == MEMORY_PROMPT:
                    assert definitions is None and options["max_tokens"] == 1024
                    payload = json.loads(messages[1]["content"])
                    assert payload["previous_memory"] == (None if not summaries else memory_value())
                    summaries.append(payload)
                    if len(summaries) == 3:
                        saved = Session(NAME).load()
                        assert saved[-1]["content"] == "SECOND_ROUTE complete"
                        assert len(trace(saved[-6:])) == 3  # 最终回答和配对结果已保存，才生成记忆。
                    return {"role": "assistant", "content": json.dumps(memory_value(), ensure_ascii=False)}
                content = next(item["content"] for item in reversed(messages) if item["role"] == "user")
                route = content.split()[0]
                assert content.count("<session_memory>") == (0 if route == "OFF_TOOL" else 1)
                requests.append(route)
                counters[route] = counters.get(route, 0) + 1
                if route == "FIRST_ROUTE":
                    return {"role": "assistant", "content": "FIRST_ROUTE complete"}
                if counters[route] == 1:
                    return message((route + "-compact", "compact_context", {}), *(
                        (("current-read", "read_file", {"file_path": "playground/source.txt"}),
                         ("duplicate-compact", "compact_context", {})) if route == "SECOND_ROUTE" else ()))
                assert ("已经登记" if route == "SECOND_ROUTE" else "已停用") in messages[-1]["content"]
                return {"role": "assistant", "content": route + " complete"}
            def execute(name, arguments, **options):
                executed.append(name)
                return execute_tool(name, arguments, **options)
            def snapshot():
                current = Session(NAME)
                history = current.load()
                record = SessionMemoryStore(current).read(current.session_memory)
                records.append(record)
                sizes.append(len(history))
                return current, history
            def inputs():
                yield "/compact"
                current, first = snapshot()
                assert first[-2:] == original[-2:] and current.session_memory["enabled"]
                yield "FIRST_ROUTE " + "新增完整轮次资料，无新动作。" * 300
                assert len(summaries) == 1  # 手动提交后的同一快照不立即自动再折叠。
                yield "SECOND_ROUTE 登记折叠并核对当前文件。"
                current, last = snapshot()
                assert len(summaries) == 3 and records[-1]["previous_id"] is not None
                assert last[-1]["content"] == "SECOND_ROUTE complete" and len(trace(last)) == 3
                yield "/compact"
                assert len(summaries) == 3  # 占位轮次加最近轮次，没有新增旧轮次。
                yield "/session-memory off"
                disabled = Session(NAME)
                off_history = disabled.load()
                yield "/compact"
                assert disabled.load() == off_history and len(summaries) == 3
                yield "OFF_TOOL 申请折叠"
                disabled.load()
                assert not disabled.session_memory["enabled"] and len(summaries) == 3
                yield "/session-memory use"
                yield "exit"
            with patch("personal_agent.main.get_model_message", side_effect=model), \
                 patch("personal_agent.main.execute_tool", side_effect=execute), patch("builtins.input", side_effect=inputs()):
                code, output, errors = invoke(["--resume", NAME, "--auto-compact-chars", "1"])
            assert code == 0 and "独立会话记忆已停用" in errors, errors
            assert "自动折叠失败" not in errors and "工具登记的折叠失败" not in errors
            store = SessionMemoryStore(session)
            all_records = [store.read(store.reference(path.stem)) for path in store.directory.glob("sm_*.json") if not path.name.endswith(".source.json")]
            assert len(all_records) == 3
            chain = {record["id"]: record for record in all_records}
            assert chain[records[-1]["previous_id"]]["previous_id"] == records[0]["id"]
            assert executed == ["compact_context", "read_file", "compact_context", "compact_context"]
            assert (root / "playground/source.txt").read_text(encoding="utf-8") == "verified-jade"
            final = Session(NAME)
            result = final.load()
            assert final.session_memory["enabled"] and final.plan_snapshot["state"] == "planning"
            assert "<session_memory>" not in str(result)
            return {"status": "passed", "validation": "fixed model/usage; actual CLI, file read and immutable records",
                "memory_requests": len(summaries), "triggers": ["manual", "automatic", "tool-after-completion"],
                "initial_messages": len(original), "first_compacted_messages": sizes[0], "model_task_routes": requests,
                "new_tool_attempts": executed, "chain_length": len(all_records), "latest_tool_batch_preserved": True,
                "off_blocks_manual_auto_and_tool": True, "duplicate_and_unchanged_snapshot_skip": True,
                "previous_memory_merged": True, "plan_and_file_preserved": True, "request_copy_only": True}


def verify_compatibility():
    calls = []
    with tempfile.TemporaryDirectory(prefix="personal-compact-compat-") as temporary:
        root = Path(temporary).resolve()
        with isolated(root), patch("personal_agent.main.get_model_settings", return_value=SETTINGS):
            text = Session("legacy-text")
            original = seed(text, legacy=True)
            assert json.loads(text.path.read_text(encoding="utf-8"))["version"] == 1
            text_memory = memory_value()
            text_memory["working_memory"]["current_challenges"] = "同一记忆也占上下文。" * 100
            lengths = {}
            def model(messages, definitions=None, *, on_usage, **_):
                on_usage({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                if messages[0]["content"] == MEMORY_PROMPT:
                    payload = json.loads(messages[1]["content"])
                    if "memory" not in calls:
                        assert "LEGACY-R11" in str(payload["messages"])
                    else:
                        assert payload["previous_memory"] == text_memory
                    calls.append("memory")
                    return {"role": "assistant", "content": json.dumps(text_memory, ensure_ascii=False)}
                calls.append("task")
                return {"role": "assistant", "content": "已完成新轮次。"}
            def inputs():
                yield "/compact"
                assert calls == [] and text.load() == original  # 旧内联摘要没有新旧轮次，不强制迁移。
                yield "NEW_ROUND " + "新增资料。" * 100
                yield "/compact"
                yield "SHORT_ROUND"
                before = text.load()
                record = SessionMemoryStore(text).read(text.session_memory)
                lengths["before_with_memory"] = context_chars(with_session_memory(before, record))
                lengths["before_history_only"] = context_chars(before)
                yield "/compact"
                after = text.load()
                record = SessionMemoryStore(text).read(text.session_memory)
                lengths["after_with_memory"] = context_chars(with_session_memory(after, record))
                assert lengths["before_history_only"] <= lengths["after_with_memory"] < lengths["before_with_memory"]
                yield "exit"
            with patch("personal_agent.main.get_model_message", side_effect=model), patch("builtins.input", side_effect=inputs()):
                code, _, errors = invoke(["--resume", "legacy-text"])
            assert code == 0 and not errors, errors
            migrated = Session("legacy-text")
            migrated.load()
            assert not migrated.with_tools and migrated.session_memory is not None
            assert json.loads(text.path.read_text(encoding="utf-8"))["version"] == 5

            # 没有具名 Session 的聊天继续走六字段摘要，不写独立记忆引用。
            anonymous_calls = []
            def anonymous_model(messages, definitions=None, *, on_usage, **_):
                on_usage({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                anonymous_calls.append(messages[0].get("content"))
                if messages[0].get("content") == SUMMARY_PROMPT:
                    return {"role": "assistant", "content": json.dumps({"goal": "匿名旧路径", "constraints": [],
                        "completed": [], "pending": [], "next_steps": [], "references": []}, ensure_ascii=False)}
                return {"role": "assistant", "content": "完整回答。"}
            with patch("personal_agent.main.get_model_message", side_effect=anonymous_model), \
                 patch("builtins.input", side_effect=["旧任务" + "匿名背景。" * 500, "最近任务", "/compact", "exit"]):
                code, output, errors = invoke([])
            assert code == 0 and not errors and anonymous_calls[-1] == SUMMARY_PROMPT
            assert "原历史备份" in output

            # 登记后普通请求失败：回滚未完成轮次，不生成记忆、不重放工具。
            failed = Session("failed-tool", with_tools=True)
            before = seed(failed)
            counter = []
            def failed_model(messages, definitions=None, *, on_usage, **_):
                counter.append(1)
                assert messages[0].get("content") != MEMORY_PROMPT
                on_usage({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                if len(counter) == 1:
                    return message(("failed-register", "compact_context", {}))
                raise RuntimeError("模拟任务未完成")
            with patch("personal_agent.main.get_model_message", side_effect=failed_model), \
                 patch("builtins.input", side_effect=["登记后失败的任务", "exit"]):
                code, _, errors = invoke(["--resume", "failed-tool"])
            restored = Session("failed-tool")
            assert code == 0 and "模拟任务未完成" in errors and restored.load() == before
            assert restored.session_memory is None and not SessionMemoryStore(restored).directory.exists()
            return {"status": "passed", "old_inline_summary_not_eagerly_migrated": True,
                "v1_text_migrates_after_successful_compaction": True, "anonymous_keeps_six_field_summary": True,
                "repeated_compaction_counts_previous_memory": lengths,
                "incomplete_task_does_not_publish_memory": True, "provider_requests": 0}


def verify_live():
    settings, requests, phases, errors_seen = get_model_settings(), [], [], []
    with tempfile.TemporaryDirectory(prefix="personal-compact-routes-live-") as temporary:
        root = Path(temporary).resolve()
        with isolated(root):
            session = Session(NAME, with_tools=True)
            original = seed(session)
            def observe(messages, definitions=None, *, on_usage, **options):
                is_memory = messages[0]["content"] == MEMORY_PROMPT
                row = {"kind": "memory" if is_memory else "task", "tools_provided": bool(definitions)}
                requests.append(row)
                def capture(reported):
                    row["usage"] = reported
                    on_usage(reported)
                result = get_model_message(messages, definitions, on_usage=capture, **options)
                row["requested_tools"] = [call["function"]["name"] for call in result.get("tool_calls", [])]
                if is_memory:
                    row["generated_memory"] = result.get("content")
                return result
            def phase(label):
                current = Session(NAME)
                history = current.load()
                phases.append({"trigger": label, "reference": deepcopy(current.session_memory), "messages": len(history)})
            def inputs():
                yield "/compact"
                phase("manual")
                yield "保留本轮资料，只回答等待，不调用工具。" + "本轮新增背景，无新动作。" * 250
                yield ("先调用 compact_context 登记折叠，再 read_file 读取 playground/source.txt；可同批调用。"
                       "只使用这两个工具，不重复登记，不写文件、不委派、不切换模式。最后简短给出记忆中的任务标识及实际文件内容。")
                phase("automatic-and-tool")
                yield "exit"
            with patch("personal_agent.main.get_model_message", side_effect=observe), patch("builtins.input", side_effect=inputs()):
                code, _, errors = invoke(["--resume", NAME, "--auto-compact-chars", "1", "--max-turns", "4", "--max-output-tokens", "2048", "--dont-ask"])
            errors_seen.append(errors)
            current = Session(NAME)
            history = current.load()
            store = SessionMemoryStore(current)
            records = [store.read(store.reference(path.stem)) for path in store.directory.glob("sm_*.json") if not path.name.endswith(".source.json")]
            answer = history[-1].get("content", "") if history else ""
            tools = trace(history)
            usage = UsageStats()
            for row in requests:
                usage.record(row.get("usage"))
            checks = {"no_cli_errors": code == 0 and not errors,
                "three_memory_requests": sum(row["kind"] == "memory" for row in requests) == 3,
                "three_published_records": len(records) == 3,
                "manual_published_before_next_question": bool(phases[0]["reference"]),
                "identifier_recalled": "ROUTE-R11" in answer, "actual_observation": "verified-jade" in answer,
                "latest_tool_pair_preserved": [row["name"] for row in tools] in (["compact_context", "read_file"], ["read_file", "compact_context"]),
                "old_tool_not_replayed": "historical-read" not in [row["id"] for row in tools],
                "file_unchanged": (root / "playground/source.txt").read_text(encoding="utf-8") == "verified-jade",
                "plan_preserved": current.plan_snapshot["state"] == "planning",
                "reference_not_written_into_history": "<session_memory>" not in str(history)}
            return {"status": "passed" if all(checks.values()) else "failed", "protocol": settings["protocol"], "model": settings["model"],
                "validation": "seeded complete history with an actual local read; real model manual/auto/tool compaction and new tool task",
                "checks": checks, "phases": phases, "requests": requests, "cli_errors": errors_seen, "answer": answer,
                "original_messages": len(original), "final_messages": len(history),
                "usage": {"calls": usage.calls, "reported_calls": usage.reported_calls, "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens, "total_tokens": usage.total_tokens, "estimated_cost": None},
                "quality_boundary": "one authored history and task, not a broad summary-quality evaluation"}
