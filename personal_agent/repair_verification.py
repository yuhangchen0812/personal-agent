"""审阅后的演示候选复验和来源绑定；只执行已知演示基线的独立副本。"""

import json
from pathlib import Path

from .ci_diagnosis import load_checked_diagnosis
from .file_runtime import plain_path, write_json_atomic
from .junit_import import load_junit_report, parse_junit
from .pytest_demo import DEMO, code_hashes, execute_pytest
from .source_snapshot import digest, snapshot_sources


def review_text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise ValueError("需提供不超过 4000 字符的实际差异审阅说明。")
    return value.strip()


def _outcomes(report, candidate, run, unchanged):
    baseline = load_junit_report(report["junit_source"]["path"])[2]
    original = {item["test"]: item["status"] for item in baseline["records"]}
    current = {item["test"]: item["status"] for item in candidate["records"]}
    addressed = {name for edit in report["diagnosis"]["edits"] for name in edit["addresses"]}
    remaining = {name for name, state in original.items() if state == "failed" and name not in addressed}
    checks = {"same_test_identities": original.keys() == current.keys(),
        "all_addressed_failures_now_pass": bool(addressed) and all(current.get(name) == "passed" for name in addressed),
        "unaddressed_failures_preserved": all(current.get(name) == "failed" for name in remaining),
        "original_passes_and_skips_preserved": all(current.get(name) == state for name, state in original.items() if state != "failed"),
        "pytest_exit_matches_remaining_failures": type(run["returncode"]) is int and run["returncode"] == (1 if remaining else 0),
        "candidate_code_unchanged_by_tests": unchanged}
    status = ("partial_fix_verified_on_demo" if remaining else "verified_on_demo") if all(checks.values()) else "not_fixed"
    return {"status": status, "all_failures_fixed": not remaining and all(checks.values()),
        "addressed": sorted(addressed), "remaining_unaddressed": sorted(remaining), "checks": checks, "summary": candidate["summary"]}


def verify_demo_proposal(report_path, review_note, *, output_dir=None):
    note = review_text(review_note)
    path = plain_path(report_path)
    report_hash = digest(path.read_bytes())
    report = load_checked_diagnosis(path)
    if ({name: info["sha256"] for name, info in report["source_manifest"].items()} != code_hashes(DEMO)
            or not report["diagnosis"]["edits"]):
        raise ValueError("复验仅支持已知 pytest 演示基线的候选；不执行任意项目。")
    directory = plain_path(output_dir if output_dir is not None else path.parent / "verification")
    source = plain_path(report["source_dir"])
    if directory.exists() or directory.is_relative_to(source) or source.is_relative_to(directory):
        raise ValueError("复验输出须为与原源码不嵌套的新目录。")
    if directory.is_relative_to(path.parent / "workspace") or directory.is_relative_to(path.parent / "candidate-source"):
        raise ValueError("复验输出不能改写诊断快照或候选。")
    candidate = snapshot_sources(path.parent / "candidate-source")
    directory.mkdir(parents=True, exist_ok=False)
    receipt = {"version": 2, "kind": "ci_repair_verification", "status": "running", "review_note": note,
        "diagnosis_report": str(path), "diagnosis_sha256": report_hash, "candidate_manifest": candidate["manifest"],
        "directory": str(directory), "new_model_calls": 0,
        "boundary": "reviewed self-authored demo only; independent pytest copy; no OS sandbox or general repair proof"}
    write_json_atomic(directory / "receipt.json", receipt)
    try:
        target = directory / "source"
        for name, raw in candidate["files"].items():
            file = target / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(raw)
        run = execute_pytest(target, directory / "candidate.xml")
        parsed = parse_junit(Path(run["xml"]).read_bytes())
        load_checked_diagnosis(path)
        if digest(path.read_bytes()) != report_hash:
            raise ValueError("复验期间诊断记录变化。")
        receipt.update(_outcomes(report, parsed, run, snapshot_sources(target)["manifest"] == candidate["manifest"]), pytest=run,
            evidence={name: digest((directory / name).read_bytes()) for name in ("candidate.xml", "candidate.log")})
    except BaseException as error:
        receipt.update(status="cancelled" if isinstance(error, KeyboardInterrupt) else "partial", error_type=type(error).__name__)
        write_json_atomic(directory / "receipt.json", receipt)
        raise
    write_json_atomic(directory / "receipt.json", receipt)
    return receipt


def load_checked_verification(receipt_path):
    path = plain_path(receipt_path)
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("version") == 4:
        from .repair_eval_gate import load_evaluation_receipt
        return load_evaluation_receipt(path, value)
    if value.get("version") == 3:
        from .configured_verification import load_configured_verification
        return load_configured_verification(path, value)
    if (value.get("version") != 2 or value.get("kind") != "ci_repair_verification" or value.get("directory") != str(path.parent)
            or value.get("status") not in {"verified_on_demo", "partial_fix_verified_on_demo"}):
        raise ValueError("需提供完整的新式修复复验回执。")
    review_text(value["review_note"])
    diagnosis_path = plain_path(value["diagnosis_report"])
    if digest(diagnosis_path.read_bytes()) != value["diagnosis_sha256"]:
        raise ValueError("复验对应的诊断记录变化。")
    report = load_checked_diagnosis(diagnosis_path)
    candidate = snapshot_sources(diagnosis_path.parent / "candidate-source")
    run = value["pytest"]
    if (candidate["manifest"] != value["candidate_manifest"] or snapshot_sources(path.parent / "source")["manifest"] != candidate["manifest"]
            or run["directory"] != str(path.parent / "source") or run["xml"] != str(path.parent / "candidate.xml")
            or run["log"] != str(path.parent / "candidate.log") or run["executed_source"] != code_hashes(path.parent / "source")
            or set(value["evidence"]) != {"candidate.xml", "candidate.log"}
            or any(digest(plain_path(path.parent / name).read_bytes()) != sha for name, sha in value["evidence"].items())):
        raise ValueError("复验的源码、XML 或日志版本变化。")
    expected = _outcomes(report, parse_junit((path.parent / "candidate.xml").read_bytes()), run, True)
    if any(value.get(key) != item for key, item in expected.items()):
        raise ValueError("复验声明与实际 XML/失败覆盖不一致。")
    from .repair_provenance import require_independent_receipt
    require_independent_receipt(report)
    return report, value
