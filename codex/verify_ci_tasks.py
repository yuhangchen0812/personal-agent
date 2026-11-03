"""CI 文件任务包：固定模型主流程/失败边界；--live 另外运行真实全部样本。"""

import argparse
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex.verify_delivery import isolated, message
from personal_agent import task_runner
from personal_agent.task_runner import load_task_pack, run_task_pack
from personal_agent.usage import TokenPrices
from personal_agent.workspace import project_root


PACKAGE = Path("examples/tasks/ci-triage")
META = "playground/input/run.json"
LOG = "playground/input/results.jsonl"
OUTPUT = "playground/output/triage.json"


def reference(run_text, log_text):
    """独立的确定性校对实现，仅供固定模型演示，不作为 Agent 工具或真实请求材料。"""
    latest, invalid = {}, []
    for line, text in enumerate(log_text.splitlines(), 1):
        if not text.strip():
            continue
        try:
            value = json.loads(text)
        except ValueError:
            invalid.append({"line": line, "reason": "invalid_json"})
            continue
        if (not isinstance(value, dict) or not isinstance(value.get("test"), str) or not value["test"].strip()
                or not isinstance(value.get("status"), str) or not isinstance(value.get("message", ""), str)):
            invalid.append({"line": line, "reason": "invalid_record"})
        elif value["status"] not in {"passed", "failed", "skipped"}:
            invalid.append({"line": line, "reason": "invalid_status"})
        else:
            latest[value["test"]] = (value, line)
    counts = {"total": len(latest), **{state: sum(value[0]["status"] == state for value in latest.values()) for state in ("passed", "failed", "skipped")}}
    failures = [{"test": name, "evidence": f"{LOG}:{line}", "message": value.get("message", "")}
        for name, (value, line) in sorted(latest.items()) if value["status"] == "failed"]
    return {"run_id": json.loads(run_text)["run_id"], "counts": counts, "failures": failures, "invalid_lines": invalid}


def fixed_model(mode="correct"):
    calls = []
    def model(messages, definitions, *, on_usage, **_):
        calls.append(deepcopy(messages))
        assert {tool["name"] for tool in definitions} == {"read_file", "list_files", "grep_search", "write_file", "edit_file"}
        assert "expected_files" not in str(messages) and "required_tools" not in str(messages)
        on_usage({"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3})
        if len(messages) == 2:
            assert "build-dev-" not in str(messages) and "build-holdout-" not in str(messages)
            return message(("meta", "read_file", {"file_path": META}), ("logs", "read_file", {"file_path": LOG}))
        results = {item["tool_call_id"]: item["content"] for item in messages if item["role"] == "tool"}
        def text(identity):
            if results[identity] == "(空文件)":
                return ""
            return re.sub(r"(?m)^[ \t]*\d+ \| ?", "", results[identity])
        if "write" not in results:
            if mode == "forbidden" and "blocked" not in results:
                return message(("blocked", "write_file", {"file_path": LOG, "content": "must not overwrite input"}))
            value = reference(text("meta"), text("logs"))
            if mode == "wrong":
                value["counts"]["total"] += 1
            return message(("write", "write_file", {"file_path": OUTPUT, "content": json.dumps(value, ensure_ascii=False)}))
        if mode == "cancel":
            raise KeyboardInterrupt()
        if mode == "skip_verify":
            return {"role": "assistant", "content": "任务已完成。"}
        if "verify" not in results:
            return message(("verify", "read_file", {"file_path": OUTPUT}))
        return {"role": "assistant", "content": "任务已完成，报告已写入并读回；CI 状态以实际产物为准。"}
    model.calls = calls
    return model


def copy_package(root):
    shutil.copytree(ROOT / PACKAGE, root / PACKAGE)


def verify_workflow():
    with tempfile.TemporaryDirectory(prefix="personal-ci-tasks-") as temporary:
        root = Path(temporary).resolve()
        copy_package(root)
        with isolated(root), patch("personal_agent.task_runner.ROOT", root):
            pack = load_task_pack(PACKAGE)
            for suite in pack["suites"].values():
                for case in suite["document"]["cases"]:
                    assert reference(case["files"][META], case["files"][LOG]) == case["expected_files"][OUTPUT]["json"]
            model = fixed_model()
            before = project_root(ROOT)
            report = run_task_pack(PACKAGE, split="all", call_model=model)
            assert report["status"] == "passed" and len(report["rows"]) == 4
            assert report["summary"] == {"development": {"selected": 2, "completed": 2, "passed": 2}, "holdout": {"selected": 2, "completed": 2, "passed": 2}}
            assert report["method_source_unchanged"] and report["suite_sources_unchanged"] and project_root(ROOT) == before
            starts = [messages for messages in model.calls if len(messages) == 2]
            assert len(starts) == 4 and len(model.calls) == 16
            for row in report["rows"]:
                reads = [item for item in row["trace"] if item["name"] == "read_file"]
                assert len(reads) == 3 and all(item["succeeded"] for item in row["trace"])
                assert all(check["passed"] for check in row["checks"])
            directory = Path(report["directory"])
            assert (directory / "report.md").is_file() and (directory / "method.md").is_file()
            return {"status": "passed", "validation": "fixed model/usage; actual parallel file reads, writes, readback, isolated workspaces and frozen goldens",
                "cases": len(report["rows"]), "split_summary": report["summary"], "logical_calls": report["usage"]["calls"],
                "scoring_data_not_in_model_requests": True, "input_and_method_unchanged": True, "workspace_scope_restored": True}


def verify_failures():
    with tempfile.TemporaryDirectory(prefix="personal-ci-task-errors-") as temporary:
        root = Path(temporary).resolve()
        copy_package(root)
        with isolated(root), patch("personal_agent.task_runner.ROOT", root):
            wrong = run_task_pack(PACKAGE, case_id="dev-dirty", call_model=fixed_model("wrong"))
            assert wrong["status"] == "failed" and wrong["rows"][0]["status"] == "complete"
            assert "任务已完成" in wrong["rows"][0]["output"] and not wrong["rows"][0]["all_passed"]
            unverified = run_task_pack(PACKAGE, case_id="dev-dirty", call_model=fixed_model("skip_verify"))
            assert unverified["status"] == "failed"
            assert any(check["kind"] == "output_read_after_last_write" and not check["passed"] for check in unverified["rows"][0]["checks"])
            forbidden = run_task_pack(PACKAGE, case_id="dev-dirty", call_model=fixed_model("forbidden"))
            assert forbidden["status"] == "failed" and "只允许修改已声明" in forbidden["rows"][0]["trace"][2]["result"]
            original = load_task_pack(PACKAGE)["suites"]["development"]["document"]["cases"][1]["files"][LOG]
            assert (Path(forbidden["directory"]) / forbidden["rows"][0]["workspace"] / LOG).read_text(encoding="utf-8") == original
            plan = run_task_pack(PACKAGE, case_id="dev-dirty", call_model=fixed_model(), permission_mode="plan")
            assert plan["status"] == "failed" and not (Path(plan["directory"]) / plan["rows"][0]["workspace"] / OUTPUT).exists()
            zero = fixed_model()
            budget = run_task_pack(PACKAGE, split="all", call_model=zero, prices=TokenPrices(Decimal(1), Decimal(1)), max_cost=Decimal(0))
            assert not zero.calls and budget["status"] == "partial" and budget["usage"]["calls"] == 0
            assert all(row["status"] == "not_run" for row in budget["rows"][1:])
            cancelled = run_task_pack(PACKAGE, split="all", call_model=fixed_model("cancel"))
            first = cancelled["rows"][0]
            assert cancelled["status"] == "cancelled" and first["score"] is None
            assert (Path(cancelled["directory"]) / first["workspace"] / OUTPUT).is_file()
            assert all(row["status"] == "not_run" for row in cancelled["rows"][1:])
            heldout_path = root / PACKAGE / "holdout.json"
            raw = heldout_path.read_bytes()
            heldout = json.loads(raw)
            heldout["cases"][0]["files"] = load_task_pack(PACKAGE)["suites"]["development"]["document"]["cases"][0]["files"]
            heldout_path.write_text(json.dumps(heldout, ensure_ascii=False), encoding="utf-8")
            try:
                run_task_pack(PACKAGE, split="all", call_model=zero)
            except ValueError as error:
                assert "不能重复" in str(error)
            else:
                raise AssertionError("duplicated heldout inputs accepted")
            finally:
                heldout_path.write_bytes(raw)
            return {"status": "passed", "wrong_artifact_overrides_done_claim": True, "declared_output_guard_preserves_inputs": True,
                "correct_artifact_without_readback_fails_trace_check": True,
                "plan_blocks_write": True, "zero_budget_blocks_all_requests": True,
                "cancelled_run_keeps_written_artifact_and_marks_rest_not_run": True, "duplicate_holdout_inputs_rejected_before_request": True}


def entrypoint():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, help="新的报告文件；默认在 acceptance-runs 下创建唯一文件")
    args = parser.parse_args()
    output = args.output or ROOT / "codex/acceptance-runs" / f"ci-verification-{uuid4().hex}.json"
    if output.exists():
        parser.error("报告文件已存在；请选择新的 --output，旧证据不会覆盖。")
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"workflow": verify_workflow(), "failures": verify_failures()}
    if args.live:
        report["live"] = run_task_pack(PACKAGE, split="all")
    report["status"] = "passed" if all(value["status"] == "passed" for value in report.values()) else "failed"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "output": str(output)}, ensure_ascii=False))
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    entrypoint()
