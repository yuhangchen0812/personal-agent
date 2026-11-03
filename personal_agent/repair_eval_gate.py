"""逐例重验公开与独立回归证据，签发/加载 v4 采纳回执；不重跑测试或模型。"""

from copy import deepcopy
from pathlib import Path

from .ci_diagnosis import load_checked_diagnosis
from .configured_tests import RUNNER_INI, test_command
from .configured_verification import load_configured_verification
from .file_runtime import plain_path, write_json_atomic
from .junit_import import load_junit_report
from .repair_eval import _dimensions, _read_reviews, case_result
from .repair_eval_suite import check_case_rules, check_inputs
from .repair_provenance import evaluation_origin
from .repair_verification import review_text
from .restricted_process import execution_limits
from .source_snapshot import digest, snapshot_sources
from .structured_output import strict_json


def _json(path):
    return strict_json(plain_path(path).read_text(encoding="utf-8"))


def _regression_run(directory, label, manifest, settings, note):
    target = directory / (label + "-regression")
    original = directory / (label + "-project")
    report = _json(target / "report.json")
    run = report["pytest"]
    config_raw = (directory / "regression-config.json").read_bytes()
    expected_source = {"path": str(directory / "regression-config.json"), "sha256": digest(config_raw), "bytes": len(config_raw)}
    if (report.get("version") != 1 or report.get("kind") != "ci_configured_test" or report.get("directory") != str(target)
            or report.get("status") not in {"tests_passed", "tests_failed"} or report["review_note"] != note
            or report["source_dir"] != str(original) or report["source_manifest"] != manifest or report["settings"] != settings
            or report["test_config_source"] != expected_source or (target / "test-config.json").read_bytes() != config_raw
            or (target / "runner.ini").read_bytes() != RUNNER_INI or snapshot_sources(original)["manifest"] != manifest
            or snapshot_sources(target / "source")["manifest"] != manifest or run["executed_manifest"] != manifest
            or run["directory"] != str(target / "source") or run["xml"] != str(target / "run.xml")
            or run["log"] != str(target / "run-runtime/pytest.log") or run["status"] != "completed"
            or type(run["returncode"]) is not int or run["returncode"] not in (0, 1)
            or run["command"] != test_command(target / "source", target, "run", settings)
            or run["limits"] != execution_limits(settings["timeout_seconds"], settings["max_output_bytes"])
            or plain_path(run["log"]).stat().st_size > settings["max_output_bytes"]):
        raise ValueError("独立回归运行的源码、配置、命令、限制或审阅绑定不一致。")
    parsed = load_junit_report(target / "run.xml")[2]
    expected = "tests_failed" if parsed["summary"]["failed"] else "tests_passed"
    if (not parsed["records"] or report["summary"] != parsed["summary"] or report["status"] != expected
            or (run["returncode"] == 0) != (parsed["summary"]["failed"] == 0)):
        raise ValueError("独立回归回执与实际 XML/退出码不一致。")
    files = [target / name for name in ("report.json", "test-config.json", "runner.ini", "run.xml", "run-runtime/pytest.log")]
    files += [root / name for root in (original, target / "source") for name in manifest]
    return parsed, report, files


def check_evaluation_case(evaluation_report, case_id):
    path = plain_path(evaluation_report)
    evaluation = _json(path)
    directory = path.parent
    if (evaluation.get("version") != 1 or evaluation.get("kind") != "ci_repair_evaluation_result"
            or evaluation.get("directory") != str(directory) or evaluation.get("status") != "evaluated"
            or evaluation.get("inputs_unchanged") is not True):
        raise ValueError("完整评估尚未完成，不能签发采纳凭据。")
    run_dir = plain_path(evaluation["source_run"])
    raw_run = (run_dir / "report.json").read_bytes()
    raw_lock = (run_dir / "inputs.lock.json").read_bytes()
    source, lock = strict_json(raw_run.decode("utf-8")), strict_json(raw_lock.decode("utf-8"))
    if (source.get("version") != 1 or source.get("kind") != "ci_repair_evaluation" or source.get("directory") != str(run_dir)
            or digest(raw_run) != evaluation["source_run_sha256"] or digest(raw_lock) != source["inputs_sha256"]
            or [(row["case_id"], row["split"]) for row in source["rows"]] != [(case["id"], case["split"]) for case in lock["cases"]]
            or [(row["case_id"], row["split"]) for row in evaluation["rows"]] != [(row["case_id"], row["split"]) for row in source["rows"]]):
        raise ValueError("评估来源、样本集合或输入锁变化。")
    rows = [row for row in source["rows"] if row["case_id"] == case_id]
    if len(rows) != 1 or not rows[0].get("candidate"):
        raise ValueError("所选案例没有可采纳候选。")
    original = rows[0]
    row = next(row for row in evaluation["rows"] if row["case_id"] == case_id)
    case = next(case for case in lock["cases"] if case["id"] == case_id)
    if case["regression_manifest"] is None:
        raise ValueError("没有提供独立回归测试，不能签发 v4 采纳凭据。")
    base = run_dir / "cases" / case_id
    # 只核验当前案例的冻结项目，允许其他独立案例已被显式采纳。
    check_inputs(run_dir, {**lock, "cases": [case]})
    origin = evaluation_origin(base / "project")
    if origin != {"run_dir": str(run_dir), "case_id": case_id, "inputs_sha256": source["inputs_sha256"]}:
        raise ValueError("诊断项目不属于该评估案例。")
    diagnosis_path = base / "diagnosis/report.json"
    diagnosis = load_checked_diagnosis(diagnosis_path)
    check_case_rules(base, case, diagnosis, source.get("rules_review_note"))
    if (digest(diagnosis_path.read_bytes()) != original["diagnosis_sha256"]
            or digest((base / "public-baseline/report.json").read_bytes()) != original["baseline_sha256"]
            or any(original.get(key) != value for key, value in _dimensions(diagnosis_path.parent, diagnosis).items())
            or any(row.get(key) != value for key, value in original.items() if key != "outcome") or row.get("reviewed") is not True):
        raise ValueError("案例摘要、原始诊断或审阅状态不一致。")
    reviews_raw = _read_reviews(plain_path(evaluation["reviews_source"]))
    if digest(reviews_raw) != evaluation["reviews_sha256"] or (directory / "reviews.json").read_bytes() != reviews_raw:
        raise ValueError("实际差异审阅记录变化。")
    reviews = strict_json(reviews_raw.decode("utf-8-sig"))
    if not isinstance(reviews, list) or len({item["case_id"] for item in reviews}) != len(reviews):
        raise ValueError("审阅列表无效。")
    selected = [item for item in reviews if item["case_id"] == case_id]
    if len(selected) != 1 or selected[0]["proposal_sha256"] != original["proposal_sha256"] or digest((base / "diagnosis/proposal.diff").read_bytes()) != original["proposal_sha256"]:
        raise ValueError("所选案例的差异审阅指纹不匹配。")
    note = review_text(selected[0]["note"])
    target = directory / case_id
    public_path = target / "public/receipt.json"
    if digest(public_path.read_bytes()) != row["public_retest"]["receipt_sha256"]:
        raise ValueError("公开复验回执版本变化。")
    public = _json(public_path)
    checked, public = load_configured_verification(public_path, public, for_evaluation=True)
    if (public["diagnosis_report"] != str(diagnosis_path) or public["review_note"] != note or checked != diagnosis
            or any(row["public_retest"].get(key) != public.get(key) for key in ("status", "checks", "summary", "remaining_unaddressed"))):
        raise ValueError("公开复验不属于所选诊断或审阅。")
    settings = {**public["settings"], "test_paths": ["evaluation_tests"]}
    if _json(target / "regression-config.json") != settings:
        raise ValueError("独立回归测试范围/运行配置变化。")
    old_manifest = {**case["project_manifest"], **{"evaluation_tests/" + key: value for key, value in case["regression_manifest"].items()}}
    new_manifest = {**public["candidate_manifest"], **{"evaluation_tests/" + key: value for key, value in case["regression_manifest"].items()}}
    before, baseline_report, old_files = _regression_run(target, "baseline", old_manifest, settings, note)
    after, candidate_report, new_files = _regression_run(target, "candidate", new_manifest, settings, note)
    expected = case_result(public, before, after)
    if any(row.get(key) != value for key, value in expected.items()):
        raise ValueError("评估结论与实际独立测试不一致。")
    for label, report in (("baseline", baseline_report), ("candidate", candidate_report)):
        if row["regression_runs"][label] != {key: report[key] for key in ("status", "summary", "directory")}:
            raise ValueError("独立回归摘要被改写。")
    if expected["outcome"] not in {"full_fix_verified", "partial_fix_verified"} or not expected["regression_clean"]:
        raise ValueError("候选存在独立回归或未满足修复声明，禁止生成采纳回执。")
    files = [path, directory / "reviews.json", target / "regression-config.json", public_path]
    files += [public_path.parent / name for name in public["evidence"]]
    files += [public_path.parent / (label + "-source") / name for label, manifest in
        (("baseline", public["baseline_manifest"]), ("candidate", public["candidate_manifest"])) for name in manifest]
    files += old_files + new_files
    evidence = {file.relative_to(directory).as_posix(): digest(plain_path(file).read_bytes()) for file in files}
    data = {"case_id": case_id, "evaluation_report": str(path), "evaluation_sha256": digest(path.read_bytes()),
        "source_run": str(run_dir), "source_run_sha256": digest(raw_run), "inputs_sha256": digest(raw_lock),
        "reviews_sha256": digest(reviews_raw), "public_receipt_sha256": digest(public_path.read_bytes()),
        "proposal_sha256": original["proposal_sha256"], "diagnosis_report": str(diagnosis_path),
        "diagnosis_sha256": digest(diagnosis_path.read_bytes()), "candidate_manifest": public["candidate_manifest"],
        "all_failures_fixed": expected["outcome"] == "full_fix_verified", "remaining_unaddressed": public["remaining_unaddressed"],
        "remaining_regression_failures": expected["regression"]["remaining_failures"], "outcome": expected["outcome"], "evidence": evidence}
    return diagnosis, data


def issue_evaluation_receipt(evaluation_report, case_id, review_note, *, output_dir):
    note = review_text(review_note)
    report, data = check_evaluation_case(evaluation_report, case_id)
    directory = plain_path(output_dir)
    source_run = Path(data["source_run"])
    evaluation_dir = Path(data["evaluation_report"]).parent
    suite_root = Path(_json(source_run / "inputs.lock.json")["suite_source"]).parent
    protected = (source_run, evaluation_dir, suite_root)
    if directory.exists() or any(directory.is_relative_to(path) or path.is_relative_to(directory) for path in protected):
        raise ValueError("采纳回执须使用不覆盖评估、源码或套件的新独立目录。")
    directory.mkdir(parents=True, exist_ok=False)
    value = {"version": 4, "kind": "ci_repair_verification", "directory": str(directory),
        "status": "verified_with_regression" if data["all_failures_fixed"] else "partial_fix_verified_with_regression",
        "review_note": note, "new_model_calls": 0, "new_test_runs": 0, **data,
        "boundary": "mechanically rechecked public and independent regression evidence for one reviewed local case; explicit apply still required; no signature or security sandbox"}
    write_json_atomic(directory / "receipt.json", value)
    load_evaluation_receipt(directory / "receipt.json", value)
    return value


def load_evaluation_receipt(path, value):
    if (value.get("version") != 4 or value.get("kind") != "ci_repair_verification" or value.get("directory") != str(path.parent)
            or value.get("status") not in {"verified_with_regression", "partial_fix_verified_with_regression"}):
        raise ValueError("独立回归采纳回执无效。")
    review_text(value["review_note"])
    raw_hash = digest(path.read_bytes())
    report, expected = check_evaluation_case(value["evaluation_report"], value["case_id"])
    if (any(value.get(key) != item for key, item in expected.items())
            or value["status"] != ("verified_with_regression" if expected["all_failures_fixed"] else "partial_fix_verified_with_regression")
            or raw_hash != digest(path.read_bytes())):
        raise ValueError("采纳回执绑定的来源、测试产物或回归结果已变化。")
    return report, deepcopy(value)
