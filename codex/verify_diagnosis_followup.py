"""父诊断接续和补据比较；固定模型、真实文件/pytest/CLI，不请求外部模型。"""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex.verify_evidence_limits import SUITE as OLD_SUITE, fixed_model as old_model, fixed_reviews as old_reviews
from codex.verify_rule_evidence import SUITE, NOTE, RULE_NOTE, fixed_model, fixed_reviews, reject
from personal_agent.ci_diagnosis import load_checked_diagnosis, run_ci_diagnosis
from personal_agent.ci_repair import apply_repair, rollback_repair
from personal_agent.diagnosis_followup import compare_diagnoses, load_comparison
from personal_agent.file_runtime import write_json_atomic
from personal_agent.repair_eval import load_run, run_evaluation, verify_evaluation
from personal_agent.repair_eval_gate import issue_evaluation_receipt
from personal_agent.source_snapshot import snapshot_sources


def fixture(root):
    old = run_evaluation(OLD_SUITE, NOTE, output_dir=root / "before", call_model=old_model(), max_turns=4)
    old_reviews(old, root / "old-reviews.json")
    verify_evaluation(root / "before", root / "old-reviews.json", output_dir=root / "old-tests")
    mapping = {"version": 1, "parents": [{"case_id": name, "diagnosis": f"before/cases/{name}/diagnosis/report.json"} for name in ("dev-metering", "holdout-tax")]}
    write_json_atomic(root / "parents.json", mapping)
    new = run_evaluation(SUITE, NOTE, rules_review_note=RULE_NOTE, parents_path=root / "parents.json", output_dir=root / "after", call_model=fixed_model(), max_turns=4)
    assert new["status"] == "awaiting_review"
    fixed_reviews(new, root / "new-reviews.json")
    measured = verify_evaluation(root / "after", root / "new-reviews.json", output_dir=root / "new-tests")
    assert all(row["outcome"] == "full_fix_verified" for row in measured["rows"])
    issue_evaluation_receipt(root / "old-tests/report.json", "dev-metering", RULE_NOTE, output_dir=root / "old-receipt")
    for name in ("dev-metering", "holdout-tax"):
        issue_evaluation_receipt(root / "new-tests/report.json", name, RULE_NOTE, output_dir=root / (name + "-receipt"))
    return mapping


def verify_workflow():
    with tempfile.TemporaryDirectory(prefix="followup-中文 空格-") as temporary:
        root = Path(temporary).resolve()
        fixture(root)
        parent = root / "before/cases/dev-metering/diagnosis/report.json"
        child = root / "after/cases/dev-metering/diagnosis/report.json"
        old_raw, child_raw = parent.read_bytes(), child.read_bytes()
        args = [sys.executable, "-X", "utf8", "-B", str(ROOT / "run_task.py"), "followup", "--parent-diagnosis", str(parent.relative_to(root)),
            "--diagnosis", str(child.relative_to(root)), "--parent-verification", "old-receipt/receipt.json", "--verification", "dev-metering-receipt/receipt.json", "--output-dir", "补据对比"]
        result = subprocess.run(args, cwd=root, capture_output=True, timeout=60)
        assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
        value = load_comparison(root / "补据对比/report.json")
        assert value["link_mode"] == "generation_bound" and value["parent_diagnosis"]["depth"] == 1
        unknown = next(row for row in value["failures"] if row["before"]["status"] == "insufficient_evidence")
        assert not unknown["before"]["addressed"] and unknown["after"]["addressed"]
        assert unknown["candidate_retest_before"] == "failure" and unknown["candidate_retest_after"] == "passed"
        assert value["rule_delta"]["added"] == ["regulated-retention"] and value["tests_before"]["all_failures_fixed"] is False
        assert value["candidate_files_changed"] == ["metering.py"]
        assert value["tests_after"]["all_failures_fixed"] is True
        tax = compare_diagnoses(root / "before/cases/holdout-tax/diagnosis/report.json", root / "after/cases/holdout-tax/diagnosis/report.json",
            output_dir=root / "tax-compare", verification=root / "holdout-tax-receipt/receipt.json")
        assert tax["tests_before"]["state"] == "not_provided" and tax["failures"][0]["candidate_retest_before"] == "not_observed"
        source = root / "after/cases/dev-metering/project"
        before = snapshot_sources(source)["files"]
        state = apply_repair(root / "dev-metering-receipt/receipt.json", RULE_NOTE, output_dir=root / "repair")
        try:
            assert state["status"] == "applied"
        finally:
            rollback_repair(root / "repair", "恢复父子接续演示项目。")
        assert snapshot_sources(source)["files"] == before
        assert parent.read_bytes() == old_raw and child.read_bytes() == child_raw
        load_comparison(root / "补据对比/report.json")
        return {"status": "passed", "parent_bound_before_generation_and_v4_adoption": True,
            "decision_missing_inputs_rules_and_actual_candidate_tests_separate": True, "no_candidate_parent_test_not_observed": True,
            "external_unicode_cwd_cli_and_byte_rollback": True, "old_records_unchanged": True, "new_network_calls": 0}


def verify_guards():
    with tempfile.TemporaryDirectory(prefix="followup-guards-") as temporary:
        root = Path(temporary).resolve()
        mapping = fixture(root)
        parent = root / "before/cases/dev-metering/diagnosis/report.json"
        child = root / "after/cases/dev-metering/diagnosis/report.json"
        source = root / "after/cases/dev-metering/project"
        base = source.parent
        raw = parent.read_bytes()
        parent.write_bytes(raw + b"\n")
        reject(lambda: apply_repair(root / "dev-metering-receipt/receipt.json", RULE_NOTE, output_dir=root / "stale"))
        assert not (root / "stale").exists()
        parent.write_bytes(raw)
        original_map = (root / "parents.json").read_bytes()
        (root / "parents.json").write_bytes(original_map + b"\n")
        reject(lambda: load_run(root / "after"))
        (root / "parents.json").write_bytes(original_map)
        changed = json.loads(original_map); changed["parents"][0]["diagnosis"] = changed["parents"][1]["diagnosis"]
        write_json_atomic(root / "wrong-map.json", changed)
        model = fixed_model()
        reject(lambda: run_evaluation(SUITE, NOTE, rules_review_note=RULE_NOTE, parents_path=root / "wrong-map.json", output_dir=root / "wrong-source", call_model=model))
        assert not model.calls and not (root / "wrong-source").exists()
        reject(lambda: run_ci_diagnosis(base / "public-baseline/run.xml", source, parent_diagnosis=parent, call_model=model, output_dir=root / "no-rules"))
        bad_xml = root / "different.xml"
        bad_xml.write_bytes((base / "public-baseline/run.xml").read_bytes().replace(b'test_started_block', b'test_other_baseline'))
        reject(lambda: run_ci_diagnosis(bad_xml, source, parent_diagnosis=parent, rules_file=base / "rules.json", rules_review_note=RULE_NOTE,
            call_model=model, output_dir=root / "wrong-baseline"))
        green_xml = root / "new-tests/dev-metering/public/candidate.xml"
        reject(lambda: run_ci_diagnosis(green_xml, source, parent_diagnosis=parent, rules_file=base / "rules.json", rules_review_note=RULE_NOTE,
            call_model=model, output_dir=root / "green-baseline"))
        assert not model.calls
        cyclic = json.loads(raw); cyclic["parent_diagnosis"] = {"path": str(parent), "sha256": "0" * 64, "depth": 1}
        write_json_atomic(parent, cyclic)
        reject(lambda: load_checked_diagnosis(child))
        parent.write_bytes(raw)
        report = compare_diagnoses(parent, child, output_dir=root / "comparison")
        path = root / "comparison/report.json"
        content = path.read_bytes(); forged = json.loads(content)
        forged["failures"][0]["candidate_retest_after"] = "passed"
        write_json_atomic(path, forged)
        reject(lambda: load_comparison(path))
        path.write_bytes(content)
        reject(lambda: compare_diagnoses(parent, child, output_dir=root / "wrong-receipt", parent_verification=root / "holdout-tax-receipt/receipt.json"))
        reject(lambda: compare_diagnoses(parent, child, output_dir=root / "comparison"))
        fixed = fixed_model()
        def changed_after_response(history, definitions, **options):
            value = fixed(history, definitions, **options)
            if options.get("response_schema") is not None:
                parent.write_bytes(raw + b"\n")
            return value
        result = run_ci_diagnosis(base / "public-baseline/run.xml", source, parent_diagnosis=parent, rules_file=base / "rules.json", rules_review_note=RULE_NOTE,
            call_model=changed_after_response, max_turns=4, output_dir=root / "late-change")
        assert result["failure"]["kind"] == "parent_changed" and not (root / "late-change/candidate-source").exists()
        parent.write_bytes(raw)
        load_comparison(path)
        return {"status": "passed", "stale_parent_and_map_block_adoption": True, "different_source_baseline_missing_rules_stop_before_calls": True,
            "cycles_and_forged_test_summaries_rejected": True, "unrelated_receipt_and_existing_outputs_rejected": True,
            "parent_changed_during_inference_no_candidate": True, "new_network_calls": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    options = parser.parse_args()
    output = options.output or ROOT / "codex/acceptance-runs" / ("diagnosis-followup-" + uuid4().hex + ".json")
    if output.exists():
        parser.error("输出已存在，未覆盖。")
    value = {"status": "passed", "workflow": verify_workflow(), "guards": verify_guards()}
    write_json_atomic(output, value)
    print(json.dumps({"status": "passed", "output": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
