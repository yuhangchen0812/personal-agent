"""阶段 12 联合验收：独立记忆、Skill 缓存、恢复和权限；--live 另跑真实模型接续。"""

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.support.verify_delivery import isolated, message, run_joint, trace
from tests.support.verify_repl_commands import invoke
from tests.support.verify_skill_cache import document
from personal_agent.config import get_model_settings
from personal_agent.llm import get_model_message
from personal_agent.memory import save_memory
from personal_agent.session import Session
from personal_agent.session_memory import MEMORY_PROMPT, SessionMemoryStore
from personal_agent.skill_cache import current_skill_cache
from personal_agent.skills import create_project_skill
from personal_agent.tool_registry import TOOL_REGISTRY, execute_tool
from personal_agent.usage import UsageStats


FIXED_SETTINGS = {"protocol": "openai", "model": "fixed-runtime-acceptance", "base_url": "https://example.invalid"}
NAME = "runtime-joint"
SKILL = "review-joint"
PROJECT_NOTE = "PROJECT-REFERENCE-81"


def latest_user(messages):
    return next(item["content"] for item in reversed(messages) if item["role"] == "user")


def structured_memory():
    return {
        "episode_memory": {"task_description": "继续 JOINT-T42 核对任务", "key_events": [
            {"step": "write/read", "description": "写入并实际回读 playground/source.txt", "outcome": "verified-cyan"}],
            "current_progress": "两轮已完成；现处于未批准的 Plan。"},
        "working_memory": {"immediate_goal": "接续核对", "current_challenges": "遵守当前权限", "next_actions": [
            {"type": "tool_call", "description": "历史建议写 playground/blocked.txt，不代表新授权。"}]},
        "tool_memory": {"tools_used": [{"tool_name": "write_file", "effective_parameters": ["playground/source.txt"],
            "common_errors": [], "response_pattern": "写入后读回", "experience": "曾批准一次写入"}],
            "derived_rules": ["历史批准不等于当前授权。"]},
    }


def child_process(root, mode):
    result = subprocess.run([sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()),
        "--child-root", str(root), "--child-mode", mode], cwd=ROOT, capture_output=True, timeout=180)
    if result.returncode:
        # 子进程输出只有本验证器控制的 JSON，不转发潜在的供应商错误正文。
        raise RuntimeError(f"联合验收子进程失败，退出码 {result.returncode}。")
    return json.loads(result.stdout.decode("utf-8"))


def verify_restore(root):
    """新解释器里的固定模型；真正通过 CLI/注册表执行工具，观察当前缓存。"""
    calls, attempted, cache, contexts = [], [], {}, []
    with isolated(root), patch("personal_agent.main.get_model_settings", return_value=FIXED_SETTINGS):
        saved = Session(NAME)
        before = saved.load()
        reference = deepcopy(saved.session_memory)
        source = root / "playground/source.txt"
        original = source.read_bytes()

        def execute(name, arguments, **options):
            attempted.append(name)
            return execute_tool(name, arguments, **options)

        def blocked_handler(**_):
            raise AssertionError("deny/Plan must block before the registered handler")

        def model(messages, definitions=None, *, on_usage, **options):
            content = latest_user(messages)
            route = content.split("_CHECK", 1)[0]
            contexts.append({"route": route, "memory_blocks": content.count("<session_memory>"),
                "project_note": PROJECT_NOTE in content})
            assert content.count("<session_memory>") == (1 if route in {"RESTORE", "ON"} else 0)
            assert (PROJECT_NOTE in content) is (route != "AFTER_CLEAR")
            on_usage({"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3})
            calls.append(route)
            if route != "RESTORE":
                return {"role": "assistant", "content": "接续状态已核对。"}
            index = calls.count(route)
            if index == 1:
                assert "JOINT-T42" in content and "verified-cyan" in content
                return message(("new-skill", "skill", {"skill_name": SKILL}))
            if index == 2:
                assert "EXTERNAL_NEW_METHOD" in messages[-1]["content"]
                assert "ORIGINAL_METHOD" not in messages[-1]["content"]
                return message(("new-write", "write_file", {"file_path": "playground/blocked.txt", "content": "forbidden"}),
                    ("new-edit", "edit_file", {"file_path": "playground/source.txt", "old_string": "verified-cyan", "new_string": "forbidden"}))
            if index == 3:
                assert all("权限拒绝" in item["content"] for item in messages[-2:])
                assert not (root / "playground/blocked.txt").exists() and source.read_bytes() == original
                return message(("new-read", "read_file", {"file_path": "playground/source.txt"}))
            assert "verified-cyan" in messages[-1]["content"]
            return {"role": "assistant", "content": "JOINT-T42：实际仍是 verified-cyan，写入与编辑均被拒绝。"}

        def inputs():
            cache["startup"] = current_skill_cache().info()
            assert cache["startup"]["cached_files"] == cache["startup"]["index_builds"] == 0
            yield f"/skills search {SKILL}"
            cache["first"] = current_skill_cache().info()
            yield f"/skills search {SKILL}"
            cache["warm"] = current_skill_cache().info()
            assert cache["first"]["definition_loads"] == cache["warm"]["definition_loads"] == 1
            assert cache["warm"]["index_builds"] == 1 and cache["warm"]["index_hits"] >= 1
            path = root / f".personal/skills/{SKILL}/SKILL.md"
            document(path, SKILL, "review joint session files", "EXTERNAL_NEW_METHOD：以当前权限为准，重新核实文件。")
            yield f"/skills search {SKILL}"
            cache["external_change"] = current_skill_cache().info()
            assert cache["external_change"]["definition_loads"] == 2 and cache["external_change"]["index_builds"] == 2
            yield "/memory use joint-context"
            yield "RESTORE_CHECK review-joint：接续核对并测试当前权限。"
            restored = Session(NAME)
            history = restored.load()
            assert history[:len(before)] == before and restored.session_memory == reference
            assert [row["id"] for row in trace(history[len(before):])] == ["new-skill", "new-write", "new-edit", "new-read"]
            cache["before_memory_toggle"] = current_skill_cache().info()
            yield "/session-memory off"
            yield f"/skills search {SKILL}"
            yield "OFF_CHECK review-joint"
            yield "/session-memory use"
            yield "ON_CHECK review-joint"
            cache["after_memory_toggle"] = current_skill_cache().info()
            assert cache["after_memory_toggle"]["definition_loads"] == 2
            assert cache["after_memory_toggle"]["index_builds"] == 2
            assert cache["after_memory_toggle"]["index_hits"] > cache["before_memory_toggle"]["index_hits"]
            yield "/clear"
            cleared = Session(NAME)
            assert cleared.load() == [] and cleared.session_memory is None
            yield "AFTER_CLEAR_CHECK review-joint"
            yield "exit"

        with patch("personal_agent.main.get_model_message", side_effect=model), \
             patch("personal_agent.main.execute_tool", side_effect=execute), \
             patch("personal_agent.main.confirm_in_terminal", side_effect=AssertionError("deny/Plan must not ask")), \
             patch.dict(TOOL_REGISTRY["write_file"], {"handler": blocked_handler}), \
             patch.dict(TOOL_REGISTRY["edit_file"], {"handler": blocked_handler}), \
             patch("builtins.input", side_effect=inputs()):
            code, _, errors = invoke(["--resume", NAME, "--yolo", "--deny-tool", "write_file"])
        assert code == 0 and not errors, errors
        assert attempted == ["skill", "write_file", "edit_file", "read_file"]
        assert source.read_bytes() == original and current_skill_cache() is None
        final = Session(NAME)
        history = final.load()
        assert len(history) == 2 and final.session_memory is None
        assert all("<session_memory>" not in str(item) and PROJECT_NOTE not in str(item) for item in history)
        return {"status": "passed", "fresh_process": True, "cache": cache, "request_contexts": contexts,
            "new_tool_attempts": attempted, "historical_tools_replayed": False,
            "explicit_deny_and_restored_plan_block_before_handlers": True,
            "memory_toggles_preserve_cache_reuse": True, "clear_unbinds_both_contexts": True,
            "artifact_unchanged": True, "scope_restored": True}


def verify_joint():
    calls, cache, executed = [], {}, []
    with tempfile.TemporaryDirectory(prefix="personal-runtime-joint-") as temporary:
        root = Path(temporary).resolve()
        with isolated(root), patch("personal_agent.main.get_model_settings", return_value=FIXED_SETTINGS):
            create_project_skill(SKILL, "review joint session files", "ORIGINAL_METHOD：读取文件并遵守当前权限。")
            save_memory("joint-context", PROJECT_NOTE)
            counters = {"BUILD": 0, "RECENT": 0}

            def model(messages, definitions=None, *, on_usage, **options):
                on_usage({"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4})
                if messages[0]["content"] == MEMORY_PROMPT:
                    calls.append("memory")
                    assert not definitions
                    payload = json.loads(messages[1]["content"])
                    assert "RECENT_CHECK" not in str(payload["messages"])
                    assert "<session_memory>" not in str(payload) and PROJECT_NOTE not in str(payload)
                    return {"role": "assistant", "content": json.dumps(structured_memory(), ensure_ascii=False)}
                content = latest_user(messages)
                route = content.split("_CHECK", 1)[0]
                calls.append(route)
                counters[route] += 1
                assert PROJECT_NOTE in content and "<session_memory>" not in content
                if route == "RECENT":
                    return (message(("old-recent-read", "read_file", {"file_path": "playground/source.txt"}))
                        if counters[route] == 1 else {"role": "assistant", "content": "最近轮次已实际回读。"})
                if counters[route] == 1:
                    assert SKILL in content
                    return message(("old-skill", "skill", {"skill_name": SKILL}))
                if counters[route] == 2:
                    assert "ORIGINAL_METHOD" in messages[-1]["content"]
                    return message(("old-write", "write_file", {"file_path": "playground/source.txt", "content": "verified-cyan"}))
                if counters[route] == 3:
                    return message(("old-read", "read_file", {"file_path": "playground/source.txt"}))
                assert "verified-cyan" in messages[-1]["content"]
                return {"role": "assistant", "content": "JOINT-T42 文件写入及读回完成。"}

            def execute(name, arguments, **options):
                executed.append(name)
                return execute_tool(name, arguments, **options)

            def inputs():
                yield f"/skills search {SKILL}"
                cache["first"] = current_skill_cache().info()
                yield f"/skills search {SKILL}"
                cache["warm"] = current_skill_cache().info()
                assert cache["warm"]["definition_loads"] == 1 and cache["warm"]["index_hits"] >= 1
                yield "/memory use joint-context"
                yield "BUILD_CHECK review-joint：JOINT-T42，写入并回读。" + "固定背景资料，无新增操作要求。" * 350
                yield "RECENT_CHECK review-joint：只读核对当前文件。"
                yield "/plan"
                yield "/session-memory compact"
                cache["after_compact"] = current_skill_cache().info()
                assert cache["after_compact"]["definition_loads"] == 1
                yield "exit"

            with patch("personal_agent.main.get_model_message", side_effect=model), \
                 patch("personal_agent.main.execute_tool", side_effect=execute), patch("builtins.input", side_effect=inputs()):
                code, _, errors = invoke(["--session", NAME, "--with-tools", "--accept-edits"])
            assert code == 0 and not errors, errors
            saved = Session(NAME)
            history = saved.load()
            record = SessionMemoryStore(saved).read(saved.session_memory)
            source = json.loads((root / record["source_archive"]).read_text(encoding="utf-8"))
            assert len(source) == 12 and len(history) == 6 and history[-4:] == source[-4:]
            assert executed == ["skill", "write_file", "read_file", "read_file", "enter_plan_mode"], executed
            assert all("<session_memory>" not in str(item) and PROJECT_NOTE not in str(item) for item in source + history)
            immutable = {path: path.read_bytes() for path in (root / f".personal/session-memory/{NAME}").glob("*.json")}
            original_session = saved.path.read_bytes()

            # 组合状态下预算仍先于请求，来源损坏仍先于模型及 MCP 初始化失败。
            with patch("personal_agent.main.get_model_message", side_effect=AssertionError("zero budget must block")), \
                 patch("personal_agent.main.execute_tool", side_effect=AssertionError("no tool before model")):
                code, output, _ = invoke(["--resume", NAME, "--prompt", "review-joint 继续", "--output-format", "json",
                    "--input-price", "1", "--output-price", "1", "--max-cost", "0"])
            budget = json.loads(output)
            assert code == 1 and budget["status"] == "failed" and budget["usage"]["calls"] == 0
            assert saved.path.read_bytes() == original_session
            invalid = structured_memory()
            invalid["episode_memory"]["key_events"][0]["step"] = 1
            def numeric_step_response(messages, definitions=None, *, on_usage, **_):
                assert messages[0]["content"] == MEMORY_PROMPT and not definitions
                on_usage({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                return {"role": "assistant", "content": json.dumps(invalid, ensure_ascii=False)}
            with patch("personal_agent.main.get_model_message", side_effect=numeric_step_response), \
                 patch("builtins.input", side_effect=["/session-memory save", "exit"]):
                code, _, errors = invoke(["--resume", NAME])
            assert code == 0 and "文本字段必须是字符串" in errors
            assert saved.path.read_bytes() == original_session
            assert set(immutable) == set((root / f".personal/session-memory/{NAME}").glob("*.json"))
            source_path = root / record["source_archive"]
            source_path.write_bytes(immutable[source_path] + b"\n")
            try:
                with patch("personal_agent.main.get_model_message") as model, patch("personal_agent.main.McpToolRuntime") as mcp:
                    code, _, errors = invoke(["--resume", NAME, "--mcp-config", "unused-config.json"])
                    assert code == 1 and "来源快照指纹" in errors
                    model.assert_not_called()
                    mcp.assert_not_called()
            finally:
                source_path.write_bytes(immutable[source_path])
            assert saved.path.read_bytes() == original_session and current_skill_cache() is None
            child = child_process(root, "fixed")
            assert all(path.read_bytes() == raw for path, raw in immutable.items())
            return {"status": "passed", "validation": "fixed model/usage; actual CLI, file tools, cache, immutable files, new Python process",
                "initial_model_callbacks": calls, "initial_tool_attempts": executed, "initial_cache": cache,
                "source_messages": len(source), "compacted_messages": len(history), "recent_messages_preserved": 4,
                "source_and_history_exclude_request_only_contexts": True,
                "zero_budget_blocks_combined_workflow": True, "corrupt_source_blocks_before_model_and_mcp": True,
                "numeric_step_rejected_without_publishing": True,
                "immutable_records_preserved": True, "restore": child}


def live_model(requests, phase):
    def model(messages, definitions=None, *, on_usage, **options):
        row = {"phase": "memory" if messages[0]["content"] == MEMORY_PROMPT else phase,
            "session_memory_blocks": latest_user(messages).count("<session_memory>"), "tools_provided": bool(definitions)}
        requests.append(row)
        def capture(reported):
            row["usage"] = reported
            on_usage(reported)
        response = get_model_message(messages, definitions, on_usage=capture, **options)
        row["requested_tools"] = [call["function"]["name"] for call in response.get("tool_calls", [])]
        if row["phase"] == "memory":
            row["generated_memory"] = response.get("content")
        return response
    return model


def verify_live_restore(root):
    requests, cache, attempted = [], {}, []
    with isolated(root):
        before = Session(NAME)
        history = before.load()
        reference = deepcopy(before.session_memory)
        source = root / "playground/source.txt"
        original = source.read_bytes()
        document(root / f".personal/skills/{SKILL}/SKILL.md", SKILL, "review joint session files",
            "LIVE_NEW_METHOD：接续时先 read_file 核对用户指定文件；只依据实际结果回答，遵守当前权限。")
        def execute(name, arguments, **options):
            attempted.append(name)
            return execute_tool(name, arguments, **options)
        def inputs():
            cache["startup"] = current_skill_cache().info()
            yield f"/skills search {SKILL}"
            yield f"/skills search {SKILL}"
            cache["warm"] = current_skill_cache().info()
            yield ("依据恢复的 session-memory，报告先前任务标识和文件观察；必须先调用 skill 加载 review-joint，"
                   "然后 read_file 读取记忆中的文件核实当前内容。只允许这两个工具，不写文件、不委派、不切换模式。最后简短报告标识与实际内容。")
            yield "exit"
        with patch("personal_agent.main.get_model_message", side_effect=live_model(requests, "restored-task")), \
             patch("personal_agent.main.execute_tool", side_effect=execute), patch("builtins.input", side_effect=inputs()):
            code, _, errors = invoke(["--resume", NAME, "--dont-ask", "--max-turns", "4", "--max-output-tokens", "512"])
        after = Session(NAME)
        result = after.load()
        new_history = result[len(history):]
        answer = new_history[-1].get("content", "") if new_history else ""
        tools = trace(new_history)
        checks = {"completed": code == 0 and not errors and bool(answer),
            "memory_in_every_parent_request_once": bool(requests) and all(row["session_memory_blocks"] == 1 for row in requests),
            "recalled_task": "JOINT-LIVE-29" in answer, "observed_file": "verified-cyan" in answer,
            "new_method_loaded": any(row["name"] == "skill" and "LIVE_NEW_METHOD" in row["result_preview"] for row in tools),
            "new_read_observed": any(row["name"] == "read_file" and "verified-cyan" in row["result_preview"] for row in tools),
            "only_requested_new_tools": attempted == ["skill", "read_file"],
            "history_prefix_and_binding_preserved": result[:len(history)] == history and after.session_memory == reference,
            "cache_rebuilt_and_reused": cache["startup"]["cached_files"] == 0 and cache["warm"]["definition_loads"] == 1 and cache["warm"]["index_hits"] >= 1,
            "artifact_unchanged": source.read_bytes() == original,
            "request_context_not_saved": all("<session_memory>" not in str(item) for item in result)}
        return {"status": "passed" if all(checks.values()) else "failed", "checks": checks, "requests": requests,
            "new_tool_attempts": attempted, "answer": answer, "cache": cache}


def verify_live():
    requests, attempted = [], []
    settings = get_model_settings()
    with tempfile.TemporaryDirectory(prefix="personal-runtime-live-") as temporary:
        root = Path(temporary).resolve()
        with isolated(root):
            (root / "playground").mkdir()
            target = root / "playground/source.txt"
            target.write_text("verified-cyan", encoding="utf-8")
            create_project_skill(SKILL, "review joint session files", "LIVE_OLD_METHOD：先 read_file 核实文件，再简短报告；遵守当前权限。")
            def execute(name, arguments, **options):
                attempted.append(name)
                return execute_tool(name, arguments, **options)
            commands = [f"/skills search {SKILL}", f"/skills search {SKILL}",
                "任务标识 JOINT-LIVE-29。先调用 skill 加载 review-joint，再 read_file 读取 playground/source.txt，报告标识与实际内容。"
                "只允许这两个工具，不写文件、不委派。以下重复背景用于验证折叠，均无新操作要求：" + "本次只做本地文件核对。" * 300,
                "当前工作先停在这里。仅回答等待下一条要求，不调用工具。", "/plan", "/session-memory compact", "exit"]
            with patch("personal_agent.main.get_model_message", side_effect=live_model(requests, "initial-task")), \
                 patch("personal_agent.main.execute_tool", side_effect=execute), patch("builtins.input", side_effect=commands):
                code, _, errors = invoke(["--session", NAME, "--with-tools", "--dont-ask", "--max-turns", "4", "--max-output-tokens", "2048"])
            saved = Session(NAME)
            history = saved.load()
            if code != 0 or errors or saved.session_memory is None:
                return {"status": "failed", "reason": "initial task or memory publication failed", "requests": requests,
                    "cli_error": errors, "exit_code": code, "memory_bound": saved.session_memory is not None}
            record = SessionMemoryStore(saved).read(saved.session_memory)
            source = json.loads((root / record["source_archive"]).read_text(encoding="utf-8"))
            child = child_process(root, "live")
            usage = UsageStats()
            for row in requests + child["requests"]:
                usage.record(row.get("usage"))
            checks = {"initial_skill_then_read": attempted == ["skill", "read_file", "enter_plan_mode"],
                "entire_source_from_real_agent": [row["name"] for row in trace(source)] == ["skill", "read_file"],
                "old_identifier_removed_from_chat_history": "JOINT-LIVE-29" not in json.dumps(history, ensure_ascii=False),
                "latest_turn_preserved": history[-2:] == source[-2:], "fresh_process_restore": child["status"] == "passed",
                "source_file_unchanged": target.read_text(encoding="utf-8") == "verified-cyan"}
            return {"status": "passed" if all(checks.values()) else "failed", "protocol": settings["protocol"], "model": settings["model"],
                "validation": "real initial Agent task, real memory generation, fresh Python process, real skill reload and file read",
                "checks": checks, "initial_requests": requests, "source_messages": len(source), "compacted_messages": len(history), "restore": child,
                "usage": {"calls": usage.calls, "reported_calls": usage.reported_calls, "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens, "total_tokens": usage.total_tokens, "estimated_cost": None},
                "quality_boundary": "one authored task and local example; no general memory-quality or candidate-improvement claim"}


def external_conditions():
    get_model_settings()  # 按现有规则加载项目 .env，只输出配置是否存在，不输出值。
    return {"docker_cli_present": shutil.which("docker") is not None, "podman_cli_present": shutil.which("podman") is not None,
        "anthropic_api_key_present": bool(os.getenv("ANTHROPIC_API_KEY", "").strip()),
        "anthropic_model_present": bool(os.getenv("ANTHROPIC_MODEL", "").strip()),
        "anthropic_remote_validated_this_run": False, "container_executed_this_run": False}


def entrypoint():
    parser = argparse.ArgumentParser(description="Fresh-process memory restore check")
    parser.add_argument("--child-root", type=Path, required=True)
    parser.add_argument("--child-mode", choices=("fixed", "live"), default="fixed")
    args = parser.parse_args()
    result = verify_live_restore(args.child_root.resolve()) if args.child_mode == "live" else verify_restore(args.child_root.resolve())
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    entrypoint()
