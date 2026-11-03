"""交付联合演示：固定模型串起真实本地组件；--live 另做一次真实只读委派/MCP任务。"""

import argparse
from contextlib import contextmanager, ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import re
import shlex
import shutil
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from personal_agent.main import run_repl
from personal_agent.mcp_runtime import McpToolRuntime
from personal_agent.session import Session
from personal_agent.single_run import run_once
from personal_agent.skill_eval import SkillEvaluator
from personal_agent.skill_evolution import SkillEvolutionStore
from personal_agent.skills import create_project_skill
from personal_agent.subagent import EXPLORE_SYSTEM_PROMPT
from personal_agent.tool_registry import execute_tool


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


@contextmanager
def isolated(root):
    with ExitStack() as stack:
        for module in ("tools", "session", "skills", "agent_config", "memory", "tool_output"):
            stack.enter_context(patch(f"personal_agent.{module}.PROJECT_ROOT", root))
        stack.enter_context(patch("personal_agent.skills.USER_SKILLS_ROOT", root / "isolated-user-skills"))
        stack.enter_context(patch("personal_agent.agent_config.USER_AGENTS_ROOT", root / "isolated-user-agents"))
        yield


def mcp_config(root):
    path = root / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"demo": {"command": "{python}", "args":
        ["-X", "utf8", "-B", "-m", "examples.mcp_server_demo"], "cwd": str(ROOT)}}}), encoding="utf-8")
    return path


def message(*calls):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}
        for identity, name, arguments in calls]}


def trace(messages):
    result, pending = [], {}
    for item in messages:
        for call in item.get("tool_calls", []):
            row = {"id": call["id"], "name": call["function"]["name"], "result_preview": None}
            result.append(row)
            pending[call["id"]] = row
        if item["role"] == "tool" and item["tool_call_id"] in pending:
            pending.pop(item["tool_call_id"])["result_preview"] = item["content"][:1000]
    return result


def run_joint():
    stages, requests, approvals, runtimes = {}, [], [], []
    phase, counters = ["build"], {"build": 0, "resume": 0, "future": 0, "child": 0}
    output, errors = io.StringIO(), io.StringIO()
    plan_text = "读取实际输入，生成汇总 JSON，修正总和，运行本地校验，再读回产物。"
    shell = "$v = Get-Content -LiteralPath 'summary.json' -Raw -Encoding utf8 | ConvertFrom-Json; if ($v.total -ne 10 -or $v.count -ne 3) { throw 'summary mismatch' }; Write-Output 'artifact-ok'"

    def runtime(configs):
        instance = McpToolRuntime(configs)
        runtimes.append(instance)
        return instance

    with tempfile.TemporaryDirectory(prefix="personal-delivery-") as temporary:
        root = Path(temporary).resolve()
        (root / "playground").mkdir()
        (root / "playground/source.json").write_text('{"numbers":[2,3,5]}', encoding="utf-8")
        target = root / "playground/summary.json"
        with isolated(root):
            skill = create_project_skill("review-summary", "核对汇总文件", "先读取实际文件，再报告核对结果。")
            original_skill = skill.read_bytes()
            config = mcp_config(root)

            def approve(name, arguments, reason):
                approvals.append(name)
                if name == "exit_plan_mode":
                    assert not target.exists() and arguments["plan"] == plan_text
                    stages["plan_approved_before_product_write"] = True
                elif name == "write_file":
                    assert arguments["file_path"] == "playground/summary.json" and not target.exists()
                elif name == "edit_file":
                    assert json.loads(target.read_text(encoding="utf-8"))["total"] == 9
                elif name == "run_shell":
                    assert arguments["command"] == shell
                else:
                    assert name == "mcp__demo__echo" and arguments == {"text": "summary-ok"}
                return True  # 仅批准本次固定演示、隔离工程中的已核对操作。

            def model(messages, definitions=None, *, on_usage=None, **_):
                child = messages[0].get("content") == EXPLORE_SYSTEM_PROMPT
                route = "child" if child else phase[0]
                n = counters[route]
                counters[route] += 1
                requests.append({"route": route, "tools": [tool["name"] for tool in definitions or []]})
                on_usage({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                if child:
                    assert "DELIVERY_PARENT_TOKEN" not in json.dumps(messages, ensure_ascii=False)
                    stages["child_history_isolated"] = True
                    if n == 0:
                        return message(("child-read", "read_file", {"file_path": "playground/summary.json"}))
                    assert "10" in messages[-1]["content"]
                    return {"role": "assistant", "content": "子任务实际读取，核对 total=10、count=3。"}
                if route == "build":
                    if n == 0:
                        return message(("enter", "enter_plan_mode", {}), ("source", "read_file", {"file_path": "playground/source.json"}))
                    plan = re.search(r"playground/\.plans/plan-[0-9a-f]{32}\.md", json.dumps(messages, ensure_ascii=False)).group()
                    if n == 1:
                        return message(("plan", "write_file", {"file_path": plan, "content": plan_text}),
                                       ("premature", "write_file", {"file_path": "playground/summary.json", "content": "must be blocked"}))
                    if n == 2:
                        assert not target.exists() and "权限拒绝" in messages[-1]["content"]
                        stages["plan_blocked_unapproved_write"] = True
                        return message(("submit", "exit_plan_mode", {}))
                    if n == 3:
                        return message(("write", "write_file", {"file_path": "playground/summary.json", "content": '{"total":9,"count":3}'}),
                                       ("edit", "edit_file", {"file_path": "playground/summary.json", "old_string": '"total":9', "new_string": '"total":10'}))
                    if n == 4:
                        return message(("shell", "run_shell", {"command": shell}))
                    if n == 5:
                        assert "退出码：0" in messages[-1]["content"] and "artifact-ok" in messages[-1]["content"]
                        stages["real_shell_verified_artifact"] = True
                        return message(("verify", "read_file", {"file_path": "playground/summary.json"}))
                    return {"role": "assistant", "content": "已按计划生成并编辑汇总，Shell 校验及文件回读通过。"}
                if route == "resume":
                    if n == 0:
                        assert "mcp__demo__echo" not in requests[-1]["tools"]
                        return message(("skill", "skill", {"skill_name": "review-summary", "args": "playground/summary.json"}))
                    if n == 1:
                        return message(("delegate", "agent", {"type": "explore", "prompt": "只读检查 playground/summary.json 的 total 和 count，报告实际值。"}))
                    if n == 2:
                        return message(("search", "tool_search", {"query": "mcp__demo__echo", "limit": 1}))
                    if n == 3:
                        assert "mcp__demo__echo" in requests[-1]["tools"]
                        return message(("echo", "mcp__demo__echo", {"text": "summary-ok"}))
                    assert "summary-ok" in messages[-1]["content"]
                    return {"role": "assistant", "content": "Skill 已加载，只读子任务核对 10/3，MCP 回显 summary-ok。"}
                if n == 0:
                    return message(("future-skill", "skill", {"skill_name": "review-summary", "args": "playground/summary.json"}))
                if n == 1:
                    assert lesson in messages[-1]["content"]
                    stages["adopted_method_loaded_in_next_task"] = True
                    return message(("future-read", "read_file", {"file_path": "playground/summary.json"}))
                return {"role": "assistant", "content": "已使用更新后的方法读取实际产物并核对。"}

            with patch("personal_agent.main.get_model_message", side_effect=model), \
                 patch("personal_agent.main.get_model_settings", return_value={"protocol": "openai", "model": "fixed-delivery", "base_url": "https://example.invalid"}), \
                 patch("personal_agent.main.confirm_in_terminal", side_effect=approve), \
                 patch("personal_agent.main.McpToolRuntime", side_effect=runtime), redirect_stdout(output), redirect_stderr(errors):
                with patch("builtins.input", side_effect=["构建汇总并验证。", "exit"]):
                    first = run_repl(session_name="delivery", with_tools=True, max_turns=12)
                assert Session("delivery").load() == first and json.loads(target.read_text(encoding="utf-8")) == {"total": 10, "count": 3}
                phase[0] = "resume"
                with patch("builtins.input", side_effect=["DELIVERY_PARENT_TOKEN：用技能、子任务和 MCP 复查结果。", "exit"]):
                    second = run_repl(session_name="delivery", resume=True, max_turns=12, mcp_config=config)
                assert second[:len(first)] == first
                assert trace(second)[:len(trace(first))] == trace(first)
                assert all(client.returncode == 0 for instance in runtimes for client in instance.clients.values())

                store = SkillEvolutionStore()
                feedback = store.record_feedback("review-summary", "以后核对数据时，分别注明输入依据、结果字段和实际验证方式。", second)
                lesson = "核对数据时分别记录输入依据、结果字段和实际验证方式；缺少证据时说明待核实。"
                decision = store.draft(feedback["id"], lambda _: {"role": "assistant", "content": json.dumps({
                    "action": "merge", "target_skill": "review-summary", "description": "", "lesson": lesson,
                    "rationale": "形成可重复使用的验证说明步骤。"}, ensure_ascii=False)}, model_name="fixed-delivery")
                candidate = store.list_records("candidate")[0]
                assert candidate["status"] == "pending" and skill.read_bytes() == original_skill
                suite = root / "suite.json"
                suite.write_text(json.dumps({"version": 1, "mode": "file_tools", "name": "delivery-development", "split": "development",
                    "skill_name": "review-summary", "cases": [{"id": "sum", "prompt": "读取 playground/input.json，将数字总和写为 playground/out.json 的 total 字段。",
                    "files": {"playground/input.json": '{"numbers":[4,6]}'}, "expected_files": {"playground/out.json": {"json": {"total": 10}}},
                    "required_tools": ["read_file", "write_file"]}]}), encoding="utf-8")
                eval_calls = []
                def replay(messages, definitions, observer):
                    observer({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                    eval_calls.append(len(messages))
                    if len(messages) == 2:
                        return message(("eval-read", "read_file", {"file_path": "playground/input.json"}))
                    if len(messages) == 4:
                        return message(("eval-write", "write_file", {"file_path": "playground/out.json", "content": '{"total":10}'}))
                    return {"role": "assistant", "content": "已生成并核对总和文件。"}
                evaluation = SkillEvaluator().evaluate_tools(candidate["id"], "suite.json", replay,
                    model_settings={"model": "fixed-delivery", "stream": False, "max_tokens": 1024}, permission_mode="acceptEdits")
                assert evaluation["decision"] == "tie_on_tasks" and all(row["all_passed"] for row in evaluation["rows"])
                assert store.candidate(candidate["id"])["status"] == "pending" and skill.read_bytes() == original_skill
                adopted = store.adopt(candidate["id"])
                snapshot = root / adopted["snapshot"]
                assert snapshot.read_bytes() == original_skill
                updated = skill.read_bytes()
                store.adopt(candidate["id"])
                assert skill.read_bytes() == updated
                phase[0] = "future"
                with patch("builtins.input", side_effect=["使用 review-summary 更新方法复查文件。", "exit"]):
                    final = run_repl(session_name="delivery", resume=True, max_turns=12)
                assert final[:len(second)] == second and Session("delivery").load() == final
            assert not errors.getvalue(), errors.getvalue()
            assert approvals == ["exit_plan_mode", "write_file", "edit_file", "run_shell", "mcp__demo__echo"]
            stages.update(session_resumed_without_replay=True, mcp_discovered_then_called=True, mcp_process_closed=True,
                          pending_until_explicit_adoption=True, snapshot_matches_original=True, duplicate_adoption_idempotent=True)
            return {"status": "passed", "validation": "fixed model/usage and approval answers; real runtime, files, PowerShell, sessions and stdio MCP",
                    "stages": stages, "approvals": approvals, "artifact": json.loads(target.read_text(encoding="utf-8")),
                    "trace": trace(final), "model_requests": {"runtime": len(requests), "draft": 1, "evaluation": len(eval_calls)},
                    "evaluation": {key: evaluation[key] for key in ("status", "decision", "mode", "rows")},
                    "skill": {"status": store.candidate(candidate["id"])["status"], "original_sha256": digest(original_skill),
                              "snapshot_sha256": digest(snapshot.read_bytes()), "updated_sha256": digest(updated)},
                    "temporary_project_removed_after_return": True,
                    "quality_boundary": "Authored development case, fixed responses; no method-quality or generalization claim."}


def run_live():
    executions, runtimes, in_agent = [], [], [False]
    with tempfile.TemporaryDirectory(prefix="personal-delivery-live-") as temporary:
        root = Path(temporary).resolve()
        (root / "playground").mkdir()
        target = root / "playground/summary.json"
        target.write_text('{"total":10,"count":3}', encoding="utf-8")
        before = target.read_bytes()
        def runtime(configs):
            instance = McpToolRuntime(configs)
            runtimes.append(instance)
            return instance
        def execute(name, arguments, **options):
            event = {"name": name, "in_agent": in_agent[0]}
            if name == "agent":
                in_agent[0] = True
            try:
                result = execute_tool(name, arguments, **options)
                event["result_preview"] = result[:800]
                executions.append(event)
                return result
            finally:
                if name == "agent":
                    in_agent[0] = False
        with isolated(root), patch("personal_agent.main.McpToolRuntime", side_effect=runtime), \
             patch("personal_agent.main.execute_tool", side_effect=execute):
            report = run_once("先调用 agent，type=explore，让它实际读取 playground/summary.json 并核对 total/count；再用 tool_search 查找 mcp__demo__echo，并调用它回显 summary-ok。最后简短报告两项实际证据。不要写文件或运行命令。",
                max_turns=7, permission_mode="dontAsk", allow=["mcp__demo__echo"], mcp_config=mcp_config(root),
                deny=["write_file", "edit_file", "run_shell", "skill", "enter_plan_mode", "exit_plan_mode", "compact_context"])
        checks = {"model_completed": report["status"] == "completed", "delegated_read": any(e["name"] == "read_file" and e["in_agent"] for e in executions),
                  "mcp_echo_called": any(e["name"] == "mcp__demo__echo" and "summary-ok" in e.get("result_preview", "") for e in executions),
                  "input_unchanged": target.read_bytes() == before,
                  "mcp_closed": bool(runtimes) and all(client.returncode == 0 for instance in runtimes for client in instance.clients.values())}
        return {"passed": all(checks.values()), "validation": "real current model API, read-only subagent, actual file and stdio MCP",
                "checks": checks, "runtime": report, "executions": executions}


def static_delivery():
    copy_sources = [shlex.split(line)[1:-1] for line in (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines() if line.startswith("COPY ")]
    assert all((ROOT / source).exists() for sources in copy_sources for source in sources)
    matrix = ROOT / "codex/最终功能对照与证据.md"
    targets = [value.split("#")[0] for value in re.findall(r'\[[^\]]+\]\(([^)]+)\)', matrix.read_text(encoding="utf-8")) if not value.startswith("http")]
    assert all((matrix.parent / value).exists() for value in targets)
    return {"docker_cli": shutil.which("docker"), "podman_cli": shutil.which("podman"), "copy_sources": copy_sources,
            "alignment_links_checked": len(targets), "container_build": "not_run", "container_run": "not_run",
            "boundary": "Static source/link/config review only; no Docker parser, image build or volume/permission verification."}


def legacy_checks():
    from codex.verify_skill_evolution import run_check as evolution
    from codex.verify_skill_task_eval import run_check as task_eval
    results = []
    for name, verify in (("verify_skill_evolution", evolution), ("verify_skill_task_eval", task_eval)):
        with tempfile.TemporaryDirectory(prefix="personal-legacy-") as temporary, isolated(Path(temporary).resolve()):
            report = verify(live=False)
        assert report["success"], name
        results.append({"script": name, "success": True, "validation": "fixed model; actual local components; no historical report overwritten"})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    report = {"date": "2026-10-29", "joint": run_joint(), "legacy": legacy_checks(), "delivery": static_delivery(),
              "live": run_live() if args.live else {"status": "not_run"}}
    path = ROOT / "codex/delivery-check-2026-10-29.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(path)
    if args.live and not report["live"]["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
