"""真实目录导入与批量 CI 运行的固定验证；--live 额外请求模型，不提供标准答案。"""

import argparse
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex.verify_ci_tasks import fixed_model
from codex.verify_delivery import isolated
from personal_agent import ci_runner
from personal_agent.ci_contract import LOG, OUTPUT
from personal_agent.ci_runner import load_ci_inputs, run_ci_logs
from personal_agent.usage import TokenPrices


EXAMPLES = ROOT / "examples/ci-logs"


def prepare(root, external):
    method = root / ci_runner.METHOD_PATH
    method.parent.mkdir(parents=True)
    method.write_bytes((ROOT / ci_runner.METHOD_PATH).read_bytes())
    shutil.copytree(EXAMPLES, external)


def fingerprints(directory):
    return {path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.rglob("*") if path.is_file()}


def verify_workflow():
    with tempfile.TemporaryDirectory(prefix="ci-import-目录 ") as temporary:
        base = Path(temporary).resolve()
        root, external = base / "project", base / "外部日志 输入"
        prepare(root, external)
        with isolated(root), patch("personal_agent.task_runner.ROOT", root):
            # 原始 UTF-8 BOM/CRLF 必须逐字节复制，工具观察仍有正确行号。
            for name in ("run.json", "results.jsonl"):
                path = external / "checkout-retry" / name
                path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
            before = fingerprints(external)
            model = fixed_model()
            report = run_ci_logs(batch_dir=external, call_model=model)
            assert report["status"] == "validated" and report["summary"] == {"selected": 2, "completed": 2, "validated": 2, "not_run": 0}
            assert report["inputs_unchanged"] and report["method_source_unchanged"] and fingerprints(external) == before
            assert [row["source_name"] for row in report["rows"]] == ["checkout-retry", "no-valid-tests"]
            assert [row["ci_observation"] for row in report["rows"]] == ["failures_reported", "no_valid_tests"]
            assert len(model.calls) == 8
            for messages in model.calls:
                assert "expected_files" not in str(messages)
                if len(messages) == 2:
                    assert "local-checkout-demo" not in str(messages) and "local-empty-demo" not in str(messages)
            for row, source in zip(report["rows"], report["inputs"]):
                assert row["score"] is None and all(check["passed"] for check in row["checks"])
                workspace = Path(report["directory"]) / row["workspace"]
                for name in ("run.json", "results.jsonl"):
                    assert (workspace / "playground/input" / name).read_bytes() == (Path(source["directory"]) / name).read_bytes()
            single = run_ci_logs(input_dir=external / "checkout-retry", call_model=fixed_model())
            assert single["mode"] == "single" and single["status"] == "validated"
            assert (Path(report["directory"]) / "report.md").is_file()
            return {"status": "passed", "validation": "fixed model/usage; actual external directory import and file execution",
                "batch_runs": 2, "batch_logical_calls": report["usage"]["calls"], "single_run_validated": True,
                "original_bom_crlf_bytes_preserved": True, "source_files_unchanged": True,
                "no_goldens_or_source_contents_in_initial_request": True, "ci_failures_and_empty_log_not_called_green": True}


def rejected(action):
    try:
        action()
    except (ValueError, OSError):
        return
    raise AssertionError("invalid input/output accepted")


def verify_guards():
    with tempfile.TemporaryDirectory(prefix="ci-import-guards-") as temporary:
        base = Path(temporary).resolve()
        root, external = base / "project", base / "inputs"
        prepare(root, external)
        with isolated(root), patch("personal_agent.task_runner.ROOT", root):
            before = fingerprints(external)
            wrong = run_ci_logs(batch_dir=external, call_model=fixed_model("wrong"))
            assert wrong["status"] == "failed" and all(row["status"] == "complete" for row in wrong["rows"])
            assert all(not row["contract_validated"] for row in wrong["rows"])
            assert all(any(check["kind"] == "counts_match_final_records" and not check["passed"] for check in row["checks"]) for row in wrong["rows"])
            fixed = fixed_model()
            def bad_evidence(*args, **kwargs):
                response = deepcopy(fixed(*args, **kwargs))
                for call in response.get("tool_calls", []):
                    if call["function"]["name"] == "write_file":
                        arguments = json.loads(call["function"]["arguments"])
                        value = json.loads(arguments["content"])
                        value["failures"][0]["evidence"] = LOG + ":1"
                        arguments["content"] = json.dumps(value, ensure_ascii=False)
                        call["function"]["arguments"] = json.dumps(arguments, ensure_ascii=False)
                return response
            evidence = run_ci_logs(input_dir=external / "checkout-retry", call_model=bad_evidence)
            checks = {check["kind"]: check["passed"] for check in evidence["rows"][0]["checks"]}
            assert evidence["status"] == "failed" and checks["report_schema"] and not checks["failures_match_final_records"]
            forbidden = run_ci_logs(input_dir=external / "checkout-retry", call_model=fixed_model("forbidden"))
            assert forbidden["status"] == "failed" and fingerprints(external) == before
            unverified = run_ci_logs(input_dir=external / "checkout-retry", call_model=fixed_model("skip_verify"))
            assert unverified["status"] == "failed" and any(check["kind"] == "output_read_after_last_write" and not check["passed"] for check in unverified["rows"][0]["checks"])
            model = fixed_model()
            zero = run_ci_logs(batch_dir=external, call_model=model, prices=TokenPrices(Decimal(1), Decimal(1)), max_cost=Decimal(0))
            assert zero["status"] == "partial" and not model.calls and zero["rows"][1]["status"] == "not_run"
            cancelled = run_ci_logs(batch_dir=external, call_model=fixed_model("cancel"))
            assert cancelled["status"] == "cancelled" and cancelled["rows"][1]["status"] == "not_run"
            assert (Path(cancelled["directory"]) / cancelled["rows"][0]["workspace"] / OUTPUT).is_file()
            fixed = fixed_model()
            log = external / "checkout-retry/results.jsonl"
            original = log.read_bytes()
            def source_change(*args, **kwargs):
                log.write_bytes(original + b"\nchanged after snapshot\n")
                return fixed(*args, **kwargs)
            changed = run_ci_logs(input_dir=external / "checkout-retry", call_model=source_change)
            assert changed["status"] == "failed" and changed["rows"][0]["contract_validated"] and not changed["inputs_unchanged"]
            log.write_bytes(original)
            saved_report = Path(changed["directory"]) / "report.json"
            saved_bytes = saved_report.read_bytes()
            no_calls = fixed_model()
            rejected(lambda: run_ci_logs(input_dir=external / "checkout-retry", output_dir=changed["directory"], call_model=no_calls))
            rejected(lambda: run_ci_logs(input_dir=external / "checkout-retry", output_dir=external / "checkout-retry/new", call_model=no_calls))
            bad = external / "missing-pair"
            bad.mkdir()
            (bad / "run.json").write_text('{"run_id":"missing"}', encoding="utf-8")
            rejected(lambda: run_ci_logs(batch_dir=external, call_model=no_calls))
            (bad / "results.jsonl").write_bytes(b"\xff")
            rejected(lambda: run_ci_logs(batch_dir=external, call_model=no_calls))
            with patch("personal_agent.ci_runner.MAX_INPUT_BYTES", 4):
                rejected(lambda: run_ci_logs(input_dir=external / "checkout-retry", call_model=no_calls))
            assert not no_calls.calls and saved_report.read_bytes() == saved_bytes
            return {"status": "passed", "wrong_counts_and_schema_valid_wrong_evidence_rejected": True,
                "input_write_blocked_and_sources_unchanged": True, "readback_required": True,
                "budget_zero_calls_and_rest_not_run": True, "cancel_keeps_artifact": True,
                "source_change_detected_against_frozen_import": True,
                "existing_nested_output_missing_pair_encoding_size_rejected_before_requests": True}


def entrypoint():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or ROOT / "codex/acceptance-runs" / f"ci-import-{uuid4().hex}.json"
    if output.exists():
        parser.error("报告已存在，未执行或覆盖。")
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"workflow": verify_workflow(), "guards": verify_guards()}
    if args.live:
        report["live"] = run_ci_logs(batch_dir=EXAMPLES)
    report["status"] = "passed" if all(value["status"] in {"passed", "validated"} for value in report.values()) else "failed"
    write = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    output.write_text(write, encoding="utf-8")
    print(json.dumps({"status": report["status"], "output": str(output)}, ensure_ascii=False))
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    entrypoint()
