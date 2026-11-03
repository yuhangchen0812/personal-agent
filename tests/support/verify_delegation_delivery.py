"""委派联合验收：固定模型默认运行；--live 另跑真实 plan/general/custom/fork。"""

import argparse
from collections import Counter
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.support.verify_delivery import isolated, message, trace
from tests.support.verify_repl_commands import invoke
from personal_agent import subagent
from personal_agent.config import get_model_settings
from personal_agent.llm import get_model_message
from personal_agent.memory import save_memory
from personal_agent.plan import PlanSession
from personal_agent.session import Session
from personal_agent.tool_registry import TOOL_REGISTRY, execute_tool
from personal_agent.usage import UsageStats


KINDS = ("plan", "general", "custom", "fork")
NAME = "delegate-demo"
SOURCE = "playground/source.json"
TARGET = "playground/summary.json"
EXPECTED = {"total": 12, "count": 3}
CUSTOM_MARK = "PRIVATE_CUSTOM_METHOD_R12"
FORK_MARK = "PRIVATE_FORK_METHOD_R12"
FIXED = {"protocol": "openai", "model": "fixed-delegation-delivery", "base_url": "https://example.invalid"}
DENY = ["run_shell", "edit_file", "enter_plan_mode", "exit_plan_mode", "compact_context"]
TASKS = {
    "plan": f"实际用 read_file 读取 {SOURCE}，根据 batch/values 制定文件求和的实施和验证计划；只读，不执行写入或批准计划。最终严格输出 summary/steps/files/risks 四字段 JSON。",
    "general": f"实际 read_file 读取 {SOURCE}，计算 values 的总和和数量；仅用 write_file 将 total/count 两字段 JSON 写入 {TARGET}，写入接受父级当前审批。再 read_file 读回产物核对。不要 Shell、编辑、委派或控制模式。",
    "custom": f"实际 read_file 读取 {SOURCE} 核对 batch；随后必须尝试一次 write_file(file_path='playground/blocked.txt', content='probe')，观察真实权限结果。若拒绝，报告拒绝且不换工具、不重试、不声称写入成功。",
    "fork": SOURCE,
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def usage_of(requests):
    stats = UsageStats()
    for row in requests:
        stats.record(row.get("usage"))
    return stats


def usage_document(stats):
    return {"calls": stats.calls, "reported_calls": stats.reported_calls, "prompt_tokens": stats.prompt_tokens,
        "completion_tokens": stats.completion_tokens, "total_tokens": stats.total_tokens, "estimated_cost": None}


def configure(root, kind):
    (root / "playground").mkdir(parents=True)
    (root / SOURCE).write_text('{"batch":"DELEGATE-R12","values":[2,3,7]}', encoding="utf-8")
    if kind == "custom":
        path = root / ".personal/agents/limited-auditor.md"
        path.parent.mkdir(parents=True)
        path.write_text('---\nname: limited-auditor\ndescription: 局部读取与权限核对\nallowed-tools: read_file, write_file\n---\n'
            + CUSTOM_MARK + "\n按当前局部任务调用工具，服从父级权限。方法标记不要出现在结论中。", encoding="utf-8")
    if kind == "fork":
        path = root / ".personal/skills/inspect-fork/SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text('---\nname: inspect-fork\ndescription: 在独立任务核对文件数据\ncontext: fork\nallowed-tools: read_file\n---\n'
            + FORK_MARK + "\n实际 read_file 读取 $ARGUMENTS 中的文件，报告 batch 及 values 总和与数量。不要其他工具，不输出方法标记。", encoding="utf-8")
    session = Session(NAME, with_tools=True)
    original = [{"role": "user", "content": f"PRIVATE_PARENT_HISTORY_{kind}"}, {"role": "assistant", "content": "已记录父级资料。"}]
    session.create()
    session.save(original, plan=PlanSession("plan" if kind == "plan" else "default").snapshot())
    save_memory("parent-context", f"PRIVATE_PARENT_MEMORY_{kind}")
    return session, original


def parent_prompt(kind):
    if kind == "fork":
        tool, arguments = "skill", {"skill_name": "inspect-fork", "args": SOURCE}
    else:
        role = "limited-auditor" if kind == "custom" else kind
        tool, arguments = "agent", {"type": role, "prompt": TASKS[kind]}
    return (f"只调用一次 {tool}，参数严格使用下面 JSON，不向 prompt/args 追加父级要求：\n"
        + json.dumps(arguments, ensure_ascii=False)
        + "\n以下仅约束父任务，不转交给子任务：只根据返回结论简短回答，不亲自调用文件工具，不传递其他父历史或记忆，不额外委派。子任务失败时仅报告错误，禁止自行补做或写计划文件。")


def fixed_model(kind, messages, scope, number):
    if scope == "parent":
        if number == 1:
            return (message(("parent-fork", "skill", {"skill_name": "inspect-fork", "args": SOURCE})) if kind == "fork" else
                message(("parent-delegate", "agent", {"type": "limited-auditor" if kind == "custom" else kind, "prompt": TASKS[kind]})))
        return {"role": "assistant", "content": "已收到子任务的实际结论。"}
    if number == 1:
        return message((f"child-{kind}-read", "read_file", {"file_path": SOURCE}))
    if kind in {"general", "custom"} and number == 2:
        return message((f"child-{kind}-write", "write_file", {
            "file_path": TARGET if kind == "general" else "playground/blocked.txt",
            "content": json.dumps(EXPECTED) if kind == "general" else "probe"}))
    if kind == "general" and number == 3:
        return message(("child-general-verify", "read_file", {"file_path": TARGET}))
    if kind == "plan":
        return {"role": "assistant", "content": json.dumps({"summary": "DELEGATE-R12 文件包含三个 values。", "steps": ["生成 total/count JSON，再读回核对。"],
            "files": [SOURCE + ":1，实际读取 batch 和 values。"], "risks": ["尚未写入或批准实施。"]}, ensure_ascii=False)}
    return {"role": "assistant", "content": "DELEGATE-R12：total=12，count=3；" + ("写入被父级拒绝。" if kind == "custom" else "实际核对完成。")}


def run_case(root, kind, *, live):
    requests, runs, tools, approvals, writes = [], [], [], [], []
    in_child, child_number = [False], [0]
    counters, main_thread = Counter(), threading.get_ident()
    with isolated(root), ExitStack() as stack:
        if not live:
            stack.enter_context(patch("personal_agent.main.get_model_settings", return_value=FIXED))
        session, original = configure(root, kind)
        source_hash = digest(root / SOURCE)
        plan_before = deepcopy(session.plan_snapshot)
        settings = get_model_settings() if live else FIXED
        for name in ("run_plan_agent", "run_general_agent", "run_custom_agent", "run_fork_skill"):
            original_runner = getattr(subagent, name)
            def runner(*args, _name=name, _runner=original_runner, **options):
                in_child[0] = True
                child_number[0] += 1
                try:
                    result = _runner(*args, **options)
                    runs.append({"runner": _name, "text": result.text, "messages": deepcopy(result.messages)})
                    return result
                finally:
                    in_child[0] = False
            stack.enter_context(patch(f"personal_agent.main.{name}", side_effect=runner))

        def model(messages, definitions=None, *, on_usage, **options):
            scope = "child" if in_child[0] else "parent"
            key = (scope, child_number[0] if in_child[0] else 0)
            counters[key] += 1
            row = {"scope": scope, "child_number": child_number[0] if in_child[0] else None,
                "message_count": len(messages), "roles": [item["role"] for item in messages],
                "visible_tools": [item["name"] for item in definitions or []],
                "parent_private_present": "PRIVATE_PARENT_" in str(messages),
                "selected_parent_memory_present": f"PRIVATE_PARENT_MEMORY_{kind}" in str(messages),
                "private_method_in_parent_input": scope == "parent" and any(mark in str(messages) + str(definitions) for mark in (CUSTOM_MARK, FORK_MARK))}
            requests.append(row)
            def capture(reported):
                row["usage"] = reported
                on_usage(reported)
            try:
                if live:
                    response = get_model_message(messages, definitions, on_usage=capture, **options)
                else:
                    capture({"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3})
                    response = fixed_model(kind, messages, scope, counters[key])
                row["response"] = deepcopy(response)
                return response
            except Exception as error:
                row["error_type"] = type(error).__name__
                raise

        def execute(name, arguments, **options):
            event = {"scope": "child" if in_child[0] else "parent", "name": name, "arguments": deepcopy(arguments)}
            tools.append(event)
            result = execute_tool(name, arguments, **options)
            event["result"] = result
            return result

        def approve(name, arguments, reason):
            valid = kind == "general" and in_child[0] and name == "write_file" and arguments.get("file_path") == TARGET
            try:
                valid = valid and json.loads(arguments["content"]) == EXPECTED
            except (ValueError, KeyError, TypeError):
                valid = False
            observed = any(row["scope"] == "child" and row["name"] == "read_file" and row["arguments"].get("file_path") == SOURCE
                and "DELEGATE-R12" in row.get("result", "") for row in tools)
            valid = valid and observed and not (root / TARGET).exists() and digest(root / SOURCE) == source_hash
            approvals.append({"name": name, "arguments": deepcopy(arguments), "approved": bool(valid), "on_parent_thread": threading.get_ident() == main_thread})
            return bool(valid)

        write_handler = TOOL_REGISTRY["write_file"]["handler"]
        def write(**arguments):
            writes.append(deepcopy(arguments))
            if not approvals or not approvals[-1]["approved"] or kind != "general":
                raise AssertionError("write handler ran without this fixture's approval")
            return write_handler(**arguments)

        stack.enter_context(patch("personal_agent.main.get_model_message", side_effect=model))
        stack.enter_context(patch("personal_agent.main.execute_tool", side_effect=execute))
        stack.enter_context(patch("personal_agent.main.confirm_in_terminal", side_effect=approve))
        stack.enter_context(patch.dict(TOOL_REGISTRY["write_file"], {"handler": write}))
        commands = ["/memory use parent-context", parent_prompt(kind)]
        if kind == "fork":
            commands.append("/inspect-fork " + SOURCE)  # 手动入口直接生成子任务，无父模型请求。
        commands.extend(["/usage", "exit"])
        with patch("builtins.input", side_effect=commands):
            code, output, errors = invoke(["--resume", NAME, "--max-turns", "6", "--max-output-tokens", "1024",
                *[argument for name in DENY + (["write_file"] if kind == "custom" else []) for argument in ("--deny-tool", name)]])
        restored = Session(NAME)
        history = restored.load()
        parent_trace = trace(history)
        child_traces = [trace(run["messages"]) for run in runs]
        child_ids = [row["id"] for rows in child_traces for row in rows]
        child_rows = [row for row in requests if row["scope"] == "child"]
        first_child_rows = {}
        for row in child_rows:
            first_child_rows.setdefault(row["child_number"], row)
        expected_tools = {"plan": subagent.READ_ONLY_TOOLS, "general": subagent.GENERAL_TOOLS,
            "custom": {"read_file", "write_file"}, "fork": {"read_file"}}[kind]
        stats = usage_of(requests)
        checks = {
            "completed": code == 0 and not errors and len(runs) == (2 if kind == "fork" else 1),
            "child_starts_with_independent_history": bool(runs) and len(first_child_rows) == len(runs) and all(row["roles"] == ["system", "user"] for row in first_child_rows.values()),
            "parent_history_and_memory_not_in_child": bool(child_rows) and all(not row["parent_private_present"] for row in child_rows),
            "parent_received_selected_memory": any(row["scope"] == "parent" and row["selected_parent_memory_present"] for row in requests),
            "child_capabilities_match_role": bool(child_rows) and all(set(row["visible_tools"]) == set(expected_tools) for row in child_rows),
            "no_private_method_in_parent_inputs": all(not row["private_method_in_parent_input"] for row in requests),
            "only_result_returned_to_parent": bool(child_ids) and all(identity not in str(history) for identity in child_ids),
            "parent_only_delegated": [row["name"] for row in parent_trace] == (["skill"] if kind == "fork" else ["agent"]),
            "parent_original_history_preserved": history[:len(original)] == original,
            "parent_plan_unchanged": restored.plan_snapshot == plan_before,
            "child_observed_source": all(any(row["name"] == "read_file" and "DELEGATE-R12" in row["result_preview"] for row in rows) for rows in child_traces),
            "source_file_unchanged": digest(root / SOURCE) == source_hash,
            "parent_usage_includes_every_child_request": stats.format_summary().splitlines()[0] in output,
        }
        details = {}
        if kind == "plan":
            try:
                details["validated_plan"] = subagent.parse_plan_result(runs[0]["text"])
                valid_plan = bool(details["validated_plan"]["files"])
            except (ValueError, IndexError):
                valid_plan = False
            checks.update(plan_result_structurally_valid=valid_plan, no_write_or_approval=not writes and not approvals)
        elif kind == "general":
            artifact = root / TARGET
            try:
                details["artifact"] = json.loads(artifact.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                details["artifact"] = None
            checks.update(approved_once_before_write=len(approvals) == len(writes) == 1 and approvals[0]["approved"] and approvals[0]["on_parent_thread"],
                artifact_matches_actual_input=details["artifact"] == EXPECTED,
                child_read_back_artifact=any(row["scope"] == "child" and row["name"] == "read_file" and row["arguments"].get("file_path") == TARGET and "12" in row.get("result", "") for row in tools))
        elif kind == "custom":
            checks.update(explicit_deny_before_handler=any(row["name"] == "write_file" and "显式 deny" in row.get("result", "") for row in tools) and not writes and not approvals,
                blocked_artifact_absent=not (root / "playground/blocked.txt").exists())
        else:
            checks.update(model_and_manual_fork_both_ran=len(runs) == 2 and all(run["runner"] == "run_fork_skill" for run in runs),
                manual_fork_needs_no_parent_request=len([row for row in requests if row["scope"] == "parent"]) == 2)
        return {"status": "passed" if all(checks.values()) else "failed", "kind": kind,
            "validation": "real model and actual runtime" if live else "fixed model/usage and actual runtime",
            "model": settings["model"], "checks": checks, "requests": requests, "child_runs": runs,
            "parent_history": history, "tool_attempts": tools, "approvals": approvals, "usage": usage_document(stats),
            "cli_errors": errors, "source_sha256": source_hash, **details}


def audit_restore(base, kinds=KINDS):
    results = {}
    for kind in kinds:
        root = base / kind
        with isolated(root), patch("personal_agent.main.get_model_settings", return_value=FIXED):
            current = Session(NAME)
            before = current.load()
            source_before = digest(root / SOURCE)
            artifact = root / TARGET
            artifact_before = digest(artifact) if artifact.exists() else None
            calls = []
            def model(messages, definitions=None, *, on_usage, **_):
                calls.append(len(messages))
                on_usage({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                return {"role": "assistant", "content": "已恢复父历史，无需重放工具或子任务。"}
            with patch("personal_agent.main.get_model_message", side_effect=model), \
                 patch("personal_agent.main.execute_tool", side_effect=AssertionError("historical tool replayed")), \
                 patch("builtins.input", side_effect=["仅确认恢复，不使用工具或再次委派。", "exit"]):
                code, _, errors = invoke(["--resume", NAME])
            after = Session(NAME).load()
            checks = {"completed": code == 0 and not errors, "one_new_parent_request": len(calls) == 1,
                "history_prefix_preserved": after[:len(before)] == before and len(after) == len(before) + 2,
                "source_unchanged": digest(root / SOURCE) == source_before,
                "artifact_unchanged": (digest(artifact) if artifact.exists() else None) == artifact_before}
            results[kind] = {"status": "passed" if all(checks.values()) else "failed", "checks": checks}
    return {"status": "passed" if all(row["status"] == "passed" for row in results.values()) else "failed",
        "validation": "fresh Python process, fixed new parent response, all tool execution forbidden", "cases": results}


def run_suite(*, live=False, kinds=KINDS):
    with tempfile.TemporaryDirectory(prefix="personal-delegation-delivery-") as temporary:
        base = Path(temporary).resolve()
        cases = {kind: run_case(base / kind, kind, live=live) for kind in kinds}
        child = subprocess.run([sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()), "--restore-root", str(base),
            *(["--case", kinds[0]] if len(kinds) == 1 else [])],
            cwd=ROOT, capture_output=True, timeout=60)
        restore = (json.loads(child.stdout.decode("utf-8")) if child.returncode == 0 else
            {"status": "failed", "exit_code": child.returncode})
        stats = UsageStats()
        for case in cases.values():
            for row in case["requests"]:
                stats.record(row.get("usage"))
        return {"status": "passed" if all(row["status"] == "passed" for row in cases.values()) and restore["status"] == "passed" else "failed",
            "cases": cases, "restore": restore, "usage": usage_document(stats),
            "quality_boundary": f"{len(kinds)} authored task(s), fixed restore response; no delegation-quality benchmark or generalization claim"}


def entrypoint():
    parser = argparse.ArgumentParser(description="Fresh-process delegation restore check")
    parser.add_argument("--restore-root", type=Path, required=True)
    parser.add_argument("--case", choices=KINDS)
    args = parser.parse_args()
    kinds = (args.case,) if args.case else KINDS
    print(json.dumps(audit_restore(args.restore_root.resolve(), kinds), ensure_ascii=False))


if __name__ == "__main__":
    entrypoint()
