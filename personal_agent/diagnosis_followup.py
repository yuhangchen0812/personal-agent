"""显式父诊断绑定与补据前后机械比较；不请求模型、不执行测试或重写旧记录。"""

import argparse
import json
from pathlib import Path
import sys

from .file_runtime import plain_path, write_json_atomic
from .junit_import import load_junit_report
from .source_snapshot import digest, snapshot_sources
from .structured_output import strict_json


def _json(path):
    return strict_json(plain_path(path).read_text(encoding="utf-8"))


def _identities(parsed):
    return {item["test"]: item["outcome"] for item in parsed["mapping"]}


def prepare_parent(path, manifest, rules, parsed=None, *, ancestry=()):
    from .ci_diagnosis import load_checked_diagnosis
    path = plain_path(path)
    raw = path.read_bytes()
    parent = load_checked_diagnosis(path, _parents=ancestry)
    addressed = {name for edit in parent["diagnosis"]["edits"] for name in edit["addresses"]}
    if parent["source_manifest"] != manifest or set(parent["failed"]) <= addressed:
        raise ValueError("父诊断必须具有同一源码快照及未覆盖的失败项。")
    if parsed is not None and _identities(load_junit_report(parent["junit_source"]["path"])[2]) != _identities(parsed):
        raise ValueError("父子诊断的基线测试身份或结果类型不同。")
    if rules is None:
        raise ValueError("补据接续必须显式提供新规则与规则审阅说明。")
    previous = parent.get("rules_source")
    if previous and (previous["sha256"] == rules["source"]["sha256"] or
            (previous["source"], previous["revision"]) == (rules["source"]["source"], rules["source"]["revision"])):
        raise ValueError("补据须有不同的规则字节及新的来源/版本标识。")
    if raw != path.read_bytes():
        raise ValueError("父诊断在检查期间变化。")
    depth = parent.get("parent_diagnosis", {}).get("depth", 0) + 1
    if depth >= 8:
        raise ValueError("补据后父子链将超过八份诊断记录。")
    return {"path": str(path), "sha256": digest(raw), "depth": depth}


def check_parent(report, rules, parsed, *, ancestry=()):
    if "parent_diagnosis" not in report:
        return
    reference = report["parent_diagnosis"]
    expected = prepare_parent(reference["path"], report["source_manifest"], rules, parsed, ancestry=ancestry)
    if reference != expected:
        raise ValueError("父诊断引用、版本或链深度变化。")


def load_parent_map(path, cases):
    path = plain_path(path)
    with path.open("rb") as handle:
        raw = handle.read(65537)
    if len(raw) > 65536:
        raise ValueError("父诊断映射超过 64 KiB。")
    value = strict_json(raw.decode("utf-8-sig"))
    if (not isinstance(value, dict) or set(value) != {"version", "parents"} or type(value["version"]) is not int or value["version"] != 1
            or not isinstance(value["parents"], list) or len(value["parents"]) != len(cases)):
        raise ValueError("父诊断映射需 version=1，并逐一覆盖本次所选案例。")
    selected, references = {case["id"]: case for case in cases}, {}
    for item in value["parents"]:
        if (not isinstance(item, dict) or set(item) != {"case_id", "diagnosis"} or not isinstance(item["case_id"], str)
                or item["case_id"] not in selected or item["case_id"] in references or not isinstance(item["diagnosis"], str) or not item["diagnosis"].strip()):
            raise ValueError("父诊断映射的案例 ID、唯一性或路径无效。")
        case = selected[item["case_id"]]
        references[item["case_id"]] = prepare_parent(path.parent / item["diagnosis"], case["project_snapshot"]["manifest"], case["rules_input"])
    return {"raw": raw, "source": {"path": str(path), "sha256": digest(raw), "bytes": len(raw)}, "references": references}


def _rules(report):
    from .rule_evidence import report_rules
    rules = report_rules(report)
    return {item["id"]: item for item in rules["value"]["rules"]} if rules is not None else {}


def _verification(path, diagnosis_path):
    if path is None:
        return {"state": "not_provided", "candidate_tests": {}, "independent": None}, None
    from .repair_verification import load_checked_verification
    path = plain_path(path)
    raw = path.read_bytes()
    _, receipt = load_checked_verification(path)
    if receipt["diagnosis_report"] != str(diagnosis_path) or receipt["diagnosis_sha256"] != digest(diagnosis_path.read_bytes()):
        raise ValueError("测试凭据不属于所选诊断。")
    independent = None
    if receipt["version"] == 4:
        evaluation_path = Path(receipt["evaluation_report"])
        public = _json(evaluation_path.parent / receipt["case_id"] / "public/receipt.json")
        row = next(row for row in _json(evaluation_path)["rows"] if row["case_id"] == receipt["case_id"])
        independent = row["regression_runs"]["candidate"]["summary"]
    elif receipt["version"] == 3:
        public = receipt
    else:
        raise ValueError("比较测试目前接受 v3 或含独立回归的 v4 凭据。")
    parsed = load_junit_report(public["runs"]["candidate"]["xml"])[2]
    if raw != path.read_bytes():
        raise ValueError("测试凭据在比较期间变化。")
    return {"state": receipt["status"], "all_failures_fixed": receipt["all_failures_fixed"], "candidate_tests": _identities(parsed),
        "summary": parsed["summary"], "independent": independent}, {"path": str(path), "sha256": digest(raw)}


def comparison_data(parent_path, diagnosis_path, parent_verification=None, verification=None):
    from .ci_diagnosis import load_checked_diagnosis
    from .rule_evidence import report_rules
    parent_path, diagnosis_path = plain_path(parent_path), plain_path(diagnosis_path)
    if parent_path == diagnosis_path:
        raise ValueError("父子诊断不能是同一记录。")
    raw_parent, raw_child = parent_path.read_bytes(), diagnosis_path.read_bytes()
    parent, child = load_checked_diagnosis(parent_path), load_checked_diagnosis(diagnosis_path)
    parsed = load_junit_report(child["junit_source"]["path"])[2]
    reference = prepare_parent(parent_path, child["source_manifest"], report_rules(child), parsed, ancestry=(diagnosis_path,))
    if "parent_diagnosis" in child and child["parent_diagnosis"] != reference:
        raise ValueError("子诊断绑定的父记录与本次选择不一致。")
    old_tests, old_receipt = _verification(parent_verification, parent_path)
    new_tests, new_receipt = _verification(verification, diagnosis_path)
    old_rules, new_rules = _rules(parent), _rules(child)
    def candidate_manifest(report):
        return snapshot_sources(Path(report["directory"]) / "candidate-source")["manifest"] if report["diagnosis"]["edits"] else report["source_manifest"]
    before_candidate, after_candidate = candidate_manifest(parent), candidate_manifest(child)
    rule_delta = {"added": sorted(new_rules.keys() - old_rules.keys()), "removed": sorted(old_rules.keys() - new_rules.keys()),
        "changed": sorted(key for key in old_rules.keys() & new_rules.keys() if old_rules[key] != new_rules[key]),
        "before_source": parent.get("rules_source"), "after_source": child.get("rules_source")}
    rows = []
    for before in parent["diagnosis"]["failures"]:
        after = next(item for item in child["diagnosis"]["failures"] if item["test"] == before["test"])
        test = before["test"]
        def decision(report, item):
            edits = [edit for edit in report["diagnosis"]["edits"] if test in edit["addresses"]]
            return {"status": item["status"], "missing_information": item.get("missing_information", []), "recommendation": item["recommendation"],
                "addressed": bool(edits), "rule_ids": sorted({identity for edit in edits for identity in edit.get("rule_ids", [])})}
        rows.append({"test": test, "before": decision(parent, before), "after": decision(child, after),
            "candidate_retest_before": old_tests["candidate_tests"].get(test, "not_observed"),
            "candidate_retest_after": new_tests["candidate_tests"].get(test, "not_observed")})
    if parent_path.read_bytes() != raw_parent or diagnosis_path.read_bytes() != raw_child:
        raise ValueError("诊断在比较期间变化。")
    return {"parent_diagnosis": reference, "diagnosis": {"path": str(diagnosis_path), "sha256": digest(raw_child)},
        "link_mode": "generation_bound" if "parent_diagnosis" in child else "explicit_existing_pair",
        "parent_verification": old_receipt, "verification": new_receipt, "rule_delta": rule_delta, "failures": rows,
        "tests_before": old_tests, "tests_after": new_tests,
        "candidate_changed": before_candidate != after_candidate,
        "candidate_files_changed": sorted(name for name in before_candidate.keys() | after_candidate.keys() if before_candidate.get(name) != after_candidate.get(name)),
        "boundary": "same source bytes and baseline test identities/outcomes; observed decisions and checked receipts only; no semantic authority or causal quality claim"}


def load_comparison(path):
    path = plain_path(path)
    raw = path.read_bytes()
    value = _json(path)
    if (value.get("version") != 1 or value.get("kind") != "ci_diagnosis_followup" or value.get("status") != "compared" or value.get("directory") != str(path.parent)
            or value.get("new_model_calls") != 0 or value.get("new_test_runs") != 0):
        raise ValueError("补据比较记录无效。")
    expected = comparison_data(value["parent_diagnosis"]["path"], value["diagnosis"]["path"],
        (value.get("parent_verification") or {}).get("path"), (value.get("verification") or {}).get("path"))
    if any(value.get(key) != item for key, item in expected.items()) or raw != path.read_bytes():
        raise ValueError("补据比较的实际来源、测试或摘要变化。")
    return value


def compare_diagnoses(parent_diagnosis, diagnosis, *, output_dir, parent_verification=None, verification=None):
    data = comparison_data(parent_diagnosis, diagnosis, parent_verification, verification)
    directory = plain_path(output_dir)
    records = [data["parent_diagnosis"], data["diagnosis"], data["parent_verification"], data["verification"]]
    protected = [Path(item["path"]).parent for item in records if item]
    for item in (data["parent_diagnosis"], data["diagnosis"]):
        report = _json(item["path"])
        protected.append(Path(report["source_dir"]))
    if directory.exists() or any(directory.is_relative_to(path) or path.is_relative_to(directory) for path in protected):
        raise ValueError("比较输出须为不覆盖来源、测试或源码的新独立目录。")
    directory.mkdir(parents=True, exist_ok=False)
    value = {"version": 1, "kind": "ci_diagnosis_followup", "directory": str(directory), "status": "compared", "new_model_calls": 0, "new_test_runs": 0, **data}
    write_json_atomic(directory / "report.json", value)
    lines = ["# 诊断补据前后比较", "", f"关联方式：{data['link_mode']}。", "", "诊断变化与实际候选测试分别列出；not_observed 不计通过。", "",
        "| 失败项 | 原判断 → 新判断 | 原候选测试 → 新候选测试 |", "| --- | --- | --- |"]
    escape = lambda text: text.replace("|", "\\|").replace("\n", " ")
    for row in data["failures"]:
        lines.append(f"| {escape(row['test'])} | {row['before']['status']} → {row['after']['status']} | {row['candidate_retest_before']} → {row['candidate_retest_after']} |")
    lines += ["", "规则新增：" + ", ".join(data["rule_delta"]["added"]), "", "[完整来源、缺失信息与测试记录](report.json)", "",
        "此比较不生成采纳凭据；已有结果的事后配对不会改写原诊断为生成时关联。"]
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    load_comparison(directory / "report.json")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-diagnosis", required=True, type=Path)
    parser.add_argument("--diagnosis", required=True, type=Path)
    parser.add_argument("--parent-verification", type=Path)
    parser.add_argument("--verification", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    options = vars(parser.parse_args(argv))
    try:
        value = compare_diagnoses(**options)
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f"补据比较失败：{error}", file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps({"status": value["status"], "link_mode": value["link_mode"], "report": str(Path(value["directory"]) / "report.md"), "new_model_calls": 0}, ensure_ascii=False))
