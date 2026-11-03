"""一次完整工作流验证历史索引、三轮链、CLI 和关键拒绝路径；不请求外部模型。"""

import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.support.verify_diagnosis_followup import fixture
from tests.support.verify_rule_evidence import RULE_NOTE, fixed_model, reject
from personal_agent.ci_diagnosis import run_ci_diagnosis
from personal_agent.diagnosis_followup import compare_diagnoses
from personal_agent.diagnosis_history import build_history, load_history
from personal_agent.file_runtime import write_json_atomic


def verify_workflow():
    with tempfile.TemporaryDirectory(prefix="history-中文 空格-") as temporary:
        root = Path(temporary).resolve()
        fixture(root)  # 已有真实 pytest / 父绑定 / 独立回归 fixture，仅构造一次。
        parent = root / "before/cases/dev-metering/diagnosis/report.json"
        child = root / "after/cases/dev-metering/diagnosis/report.json"
        raw_parent, raw_child = parent.read_bytes(), child.read_bytes()
        compare_diagnoses(parent, child, output_dir=root / "comparison", parent_verification=root / "old-receipt/receipt.json",
            verification=root / "dev-metering-receipt/receipt.json")
        write_json_atomic(root / "evidence.json", {"version": 1, "verifications": [], "comparisons": ["comparison/report.json"]})
        args = [sys.executable, "-X", "utf8", "-B", str(ROOT / "run_task.py"), "history", "--diagnosis", str(child.relative_to(root)),
            "--evidence", "evidence.json", "--output-dir", "历次索引"]
        process = subprocess.run(args, cwd=root, capture_output=True, timeout=60)
        assert process.returncode == 0, process.stderr.decode("utf-8", errors="replace")
        value = load_history(root / "历次索引/report.json")
        assert value["summary"]["rounds"] == 2 and value["summary"]["generation_bound_edges"] == 1
        assert value["summary"]["latest_all_failures_fixed"] is True and value["summary"]["latest_candidate_failed_tests"] == 0
        assert value["rounds"][0]["uncovered_by_proposal"] == 1 and value["rounds"][1]["uncovered_by_proposal"] == 0
        assert value["rounds"][1]["rule_delta"]["added"] == ["regulated-retention"]
        assert "<details>" in (root / "历次索引/index.html").read_text(encoding="utf-8")
        assert subprocess.run(args, cwd=root, capture_output=True, timeout=60).returncode == 2

        # 构造一轮仍缺依据的固定诊断，再用新 revision 接续，验证三轮真实父链及未知测试状态。
        base = child.parent.parent
        fixed = fixed_model()
        def partial(history, definitions, **options):
            response = fixed(history, definitions, **options)
            if options.get("response_schema") is not None:
                data = json.loads(response["content"])
                data["edits"] = [edit for edit in data["edits"] if not edit["rule_ids"]]
                for failure in data["failures"]:
                    if "test_started_block" not in failure["test"]:
                        failure.update(status="insufficient_evidence", missing_information=["固定场景尚待规则维护方进一步确认。"])
                response["content"] = json.dumps(data, ensure_ascii=False)
            return response
        second = run_ci_diagnosis(base / "public-baseline/run.xml", base / "project", parent_diagnosis=parent,
            rules_file=base / "rules.json", rules_review_note=RULE_NOTE, call_model=partial, max_turns=4, output_dir=root / "second")
        assert second["status"] == "evidence_checked"
        rules = json.loads((base / "rules.json").read_bytes()); rules["revision"] = "demo-rule-v2"
        write_json_atomic(root / "rules-v2.json", rules)
        third = run_ci_diagnosis(base / "public-baseline/run.xml", base / "project", parent_diagnosis=root / "second/report.json",
            rules_file=root / "rules-v2.json", rules_review_note=RULE_NOTE, call_model=fixed_model(), max_turns=4, output_dir=root / "third")
        assert third["status"] == "evidence_checked"
        write_json_atomic(root / "three-evidence.json", {"version": 1, "comparisons": [], "verifications": [{"diagnosis": str(parent), "receipt": "old-receipt/receipt.json"}]})
        three = build_history(root / "third/report.json", evidence=root / "three-evidence.json", output_dir=root / "three-rounds")
        assert three["summary"]["rounds"] == 3 and three["summary"]["generation_bound_edges"] == 2
        assert three["summary"]["latest_uncovered_by_proposal"] == 0 and three["summary"]["latest_all_failures_fixed"] is None
        assert three["summary"]["latest_candidate_failed_tests"] is None and three["summary"]["rounds_with_verified_tests"] == 1
        assert three["rounds"][1]["uncovered_by_proposal"] == 1

        path = root / "three-rounds/report.json"
        raw = path.read_bytes(); forged = json.loads(raw); forged["summary"]["latest_all_failures_fixed"] = True
        write_json_atomic(path, forged); reject(lambda: load_history(path)); path.write_bytes(raw)
        html = root / "three-rounds/index.html"
        content = html.read_bytes(); html.write_bytes(content + b"<h1>passed</h1>")
        reject(lambda: load_history(path)); html.write_bytes(content)
        parent.write_bytes(raw_parent + b"\n")
        reject(lambda: build_history(root / "third/report.json", output_dir=root / "stale"))
        assert not (root / "stale").exists(); parent.write_bytes(raw_parent)
        compare_diagnoses(root / "before/cases/holdout-tax/diagnosis/report.json", root / "after/cases/holdout-tax/diagnosis/report.json", output_dir=root / "unrelated")
        write_json_atomic(root / "unrelated.json", {"version": 1, "verifications": [], "comparisons": ["unrelated/report.json"]})
        reject(lambda: build_history(child, evidence=root / "unrelated.json", output_dir=root / "bad-evidence"))
        write_json_atomic(root / "wrong-receipt.json", {"version": 1, "comparisons": [], "verifications": [{"diagnosis": str(child), "receipt": "holdout-tax-receipt/receipt.json"}]})
        reject(lambda: build_history(child, evidence=root / "wrong-receipt.json", output_dir=root / "bad-receipt"))
        load_history(path)
        assert parent.read_bytes() == raw_parent and child.read_bytes() == raw_child
        return {"status": "passed", "two_and_three_round_generation_bound_chains": True, "actual_receipts_and_absent_tests_separate": True,
            "rules_uncovered_items_candidate_files_and_collapsible_index": True, "external_unicode_cwd_cli_and_no_overwrite": True,
            "stale_parent_unrelated_evidence_receipt_and_forged_json_or_html_rejected": True, "one_shared_actual_pytest_fixture": True, "new_network_calls": 0}
