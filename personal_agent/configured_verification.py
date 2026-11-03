"""外部 Python 项目：同配置重现基线、独立候选复验和 v3 来源绑定。"""

from pathlib import Path

from .ci_diagnosis import load_checked_diagnosis
from .configured_tests import RUNNER_INI, copy_snapshot, execute_configured_tests, load_test_config, test_command
from .file_runtime import plain_path, write_json_atomic
from .junit_import import load_junit_report
from .repair_verification import _outcomes, review_text
from .restricted_process import execution_limits
from .source_snapshot import PREFIX, digest, snapshot_sources


def _identities(parsed):
    return {item["test"]: item["outcome"] for item in parsed["mapping"]}


def _result(report, baseline, candidate, run, unchanged):
    result = _outcomes(report, candidate, run, unchanged)
    original = load_junit_report(report["junit_source"]["path"])[2]
    result["checks"]["baseline_reproduces_original_report"] = _identities(original) == _identities(baseline)
    okay = all(result["checks"].values())
    result["status"] = ("verified_with_test_config" if not result["remaining_unaddressed"] else "partial_fix_verified_with_test_config") if okay else "not_fixed"
    result["all_failures_fixed"] = okay and not result["remaining_unaddressed"]
    return result


def _run_checked(directory, label, settings, manifest, run):
    source = directory / (label + "-source")
    if (run.get("status") != "completed" or type(run.get("returncode")) is not int or run["returncode"] not in (0, 1)
            or run["directory"] != str(source) or run["xml"] != str(directory / (label + ".xml"))
            or run["log"] != str(directory / (label + "-runtime/pytest.log"))
            or run["command"] != test_command(source, directory, label, settings)
            or run["limits"] != execution_limits(settings["timeout_seconds"], settings["max_output_bytes"])
            or run["executed_manifest"] != manifest or snapshot_sources(source)["manifest"] != manifest
            or plain_path(run["log"]).stat().st_size > settings["max_output_bytes"]):
        raise ValueError("复验运行未完成，或命令、限制、执行副本及日志版本不一致。")
    parsed = load_junit_report(directory / (label + ".xml"))[2]
    if not parsed["records"] or (run["returncode"] == 0) != (parsed["summary"]["failed"] == 0):
        raise ValueError("测试退出码和实际 XML 不一致。")
    return parsed


def verify_configured_proposal(report_path, test_config, review_note, *, output_dir=None):
    note = review_text(review_note)
    path = plain_path(report_path)
    report_hash = digest(path.read_bytes())
    report = load_checked_diagnosis(path)
    if not report["diagnosis"]["edits"]:
        raise ValueError("诊断没有可复验的候选改动。")
    baseline = snapshot_sources(path.parent / "workspace" / PREFIX)
    candidate = snapshot_sources(path.parent / "candidate-source")
    config_raw, settings, config_source = load_test_config(test_config, baseline["manifest"])
    # 测试配置中的选定用例文件不可被候选改写，目录中的普通实现仍按已有 AST 规则处理。
    for name in settings["test_paths"]:
        if name in baseline["files"] and candidate["files"][name] != baseline["files"][name]:
            raise ValueError("候选不能改写显式选中的测试文件。")
    directory = plain_path(output_dir if output_dir is not None else path.parent / "verification")
    source = Path(report["source_dir"])
    if (directory.exists() or directory.is_relative_to(source) or source.is_relative_to(directory)
            or directory.is_relative_to(path.parent / "workspace") or directory.is_relative_to(path.parent / "candidate-source")):
        raise ValueError("复验输出须为不改写原源码、快照或候选的新目录。")
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "test-config.json").write_bytes(config_raw)
    (directory / "runner.ini").write_bytes(RUNNER_INI)
    value = {"version": 3, "kind": "ci_repair_verification", "status": "running", "directory": str(directory),
        "review_note": note, "diagnosis_report": str(path), "diagnosis_sha256": report_hash,
        "candidate_manifest": candidate["manifest"], "baseline_manifest": baseline["manifest"],
        "test_config_source": config_source, "settings": settings, "new_model_calls": 0, "runs": {},
        "boundary": "reviewed local pytest code in copies; Windows Job resource limits; no filesystem/network security sandbox"}
    write_json_atomic(directory / "receipt.json", value)
    try:
        for label, snapshot in (("baseline", baseline), ("candidate", candidate)):
            copy_snapshot(snapshot, directory / (label + "-source"))
            run = execute_configured_tests(directory / (label + "-source"), directory, label, settings)
            value["runs"][label] = run
            write_json_atomic(directory / "receipt.json", value)
            parsed = _run_checked(directory, label, settings, snapshot["manifest"], run)
            if label == "baseline":
                original = load_junit_report(report["junit_source"]["path"])[2]
                if _identities(parsed) != _identities(original) or run["returncode"] != 1:
                    value.update(status="baseline_mismatch", baseline_summary=parsed["summary"])
                    break
                actual_baseline = parsed
            else:
                value.update(_result(report, actual_baseline, parsed, run, True), baseline_summary=actual_baseline["summary"])
        load_checked_diagnosis(path)
        if digest(path.read_bytes()) != report_hash or load_test_config(test_config, baseline["manifest"])[2] != config_source:
            raise ValueError("复验期间诊断或测试配置变化。")
        value["evidence"] = {name: digest((directory / name).read_bytes()) for name in
            ("test-config.json", "runner.ini", "baseline.xml", "baseline-runtime/pytest.log", "candidate.xml", "candidate-runtime/pytest.log")
            if (directory / name).is_file()}
    except (Exception, KeyboardInterrupt) as error:
        value.update(status="cancelled" if isinstance(error, KeyboardInterrupt) else "partial", error_type=type(error).__name__)
    write_json_atomic(directory / "receipt.json", value)
    return value


def load_configured_verification(path, value, *, for_evaluation=False):
    if (value.get("kind") != "ci_repair_verification" or value.get("version") != 3 or value.get("directory") != str(path.parent)
            or value.get("status") not in {"verified_with_test_config", "partial_fix_verified_with_test_config"}):
        raise ValueError("配置复验尚未完整通过。")
    review_text(value["review_note"])
    diagnosis_path = plain_path(value["diagnosis_report"])
    if digest(diagnosis_path.read_bytes()) != value["diagnosis_sha256"]:
        raise ValueError("配置复验对应的诊断记录变化。")
    report = load_checked_diagnosis(diagnosis_path)
    directory = path.parent
    baseline = snapshot_sources(diagnosis_path.parent / "workspace" / PREFIX)
    candidate = snapshot_sources(diagnosis_path.parent / "candidate-source")
    raw, settings, config_source = load_test_config(value["test_config_source"]["path"], baseline["manifest"])
    if (config_source != value["test_config_source"] or raw != (directory / "test-config.json").read_bytes()
            or settings != value["settings"] or baseline["manifest"] != value["baseline_manifest"]
            or candidate["manifest"] != value["candidate_manifest"] or (directory / "runner.ini").read_bytes() != RUNNER_INI
            or set(value["runs"]) != {"baseline", "candidate"}):
        raise ValueError("测试配置、执行器或源码版本变化。")
    expected_names = {"test-config.json", "runner.ini", "baseline.xml", "baseline-runtime/pytest.log", "candidate.xml", "candidate-runtime/pytest.log"}
    if set(value["evidence"]) != expected_names or any(digest(plain_path(directory / name).read_bytes()) != sha for name, sha in value["evidence"].items()):
        raise ValueError("复验产物指纹变化。")
    old = _run_checked(directory, "baseline", settings, baseline["manifest"], value["runs"]["baseline"])
    new = _run_checked(directory, "candidate", settings, candidate["manifest"], value["runs"]["candidate"])
    expected = _result(report, old, new, value["runs"]["candidate"], True)
    if any(value.get(key) != item for key, item in expected.items()) or value["baseline_summary"] != old["summary"]:
        raise ValueError("配置复验声明与实际测试记录不一致。")
    if not for_evaluation:
        from .repair_provenance import require_independent_receipt
        require_independent_receipt(report)
    return report, value
