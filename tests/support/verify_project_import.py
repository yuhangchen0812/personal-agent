"""一次验证直接项目/JUnit 导入、完整任务、无独立测试降级及关键来源拒绝。"""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.support.verify_evidence_limits import fixed_model as old_model
from tests.support.verify_rule_evidence import SUITE, NOTE, RULE_NOTE, fixed_model, fixed_reviews, reject
from personal_agent.ci_diagnosis import run_ci_diagnosis
from personal_agent.configured_tests import run_project_tests
from personal_agent.project_import import create_project_task
from personal_agent.repair_eval_gate import issue_evaluation_receipt
from personal_agent.repair_workflow import task_action
from personal_agent.source_snapshot import snapshot_sources


def verify_workflow():
    with tempfile.TemporaryDirectory(prefix="project-import-中文 空格-") as temporary:
        root = Path(temporary).resolve()
        source = root / "外部项目"
        regression = root / "独立测试"
        shutil.copytree(SUITE.parent / "dev-metering/project", source)
        shutil.copytree(SUITE.parent / "dev-metering/regression", regression)
        rules = SUITE.parent / "dev-metering/rules.json"
        config = SUITE.parent / "test-config.json"
        before = snapshot_sources(source)["files"]
        baseline = run_project_tests(source, config, NOTE, output_dir=root / "baseline")
        xml = root / "baseline/run.xml"
        xml_raw = xml.read_bytes()
        parent = run_ci_diagnosis(xml, source, call_model=old_model(), max_turns=4, output_dir=root / "parent")
        assert parent["repair_scope"]["status"] == "partial_proposal"
        args = [sys.executable, "-X", "utf8", "-B", str(ROOT / "run_task.py"), "workflow", "create-project", "--source-dir", "外部项目",
            "--regression-tests", "独立测试", "--junit-report", "baseline/run.xml", "--rules", str(rules), "--rules-review-note", RULE_NOTE,
            "--parent-diagnosis", "parent/report.json", "--review-note", NOTE, "--max-turns", "4", "--output-dir", "项目任务"]
        process = subprocess.run(args, cwd=root, capture_output=True, timeout=60)
        assert process.returncode == 0, process.stderr.decode("utf-8", errors="replace")
        task = root / "项目任务"
        assert json.loads(process.stdout)["status"] == "imported"
        state = task_action(task, "diagnose", call_model=fixed_model())
        run = json.loads(Path(state["diagnosis"]["path"]).read_bytes())
        assert run["rows"][0]["provided_junit_matches_baseline"] is True
        assert run["rows"][0]["candidate"] is True
        fixed_reviews(run, root / "reviews.json")
        task_action(task, "verify", reviews=root / "reviews.json")
        state = task_action(task, "publish", review_note=RULE_NOTE)
        report = json.loads(Path(state["publication"]["path"]).read_bytes())
        assert report["all_cases_full_fix_verified"] and report["cases"][0]["receipt"]
        assert json.loads(Path(report["cases"][0]["history"]["path"]).read_bytes())["summary"]["rounds"] == 2

        # 无独立测试也能复验，但不能自动补造“独立通过”或得到采纳凭据。
        public = root / "公开任务"
        create_project_task(source, NOTE, output_dir=public, rules_file=rules, rules_review_note=RULE_NOTE, junit_report=xml, max_turns=4)
        pub_state = task_action(public, "diagnose", call_model=fixed_model())
        pub_run = json.loads(Path(pub_state["diagnosis"]["path"]).read_bytes())
        fixed_reviews(pub_run, root / "public-reviews.json")
        pub_state = task_action(public, "verify", reviews=root / "public-reviews.json")
        measured = json.loads(Path(pub_state["evaluation"]["path"]).read_bytes())
        assert measured["rows"][0]["outcome"] == "public_fix_verified_without_independent"
        reject(lambda: issue_evaluation_receipt(pub_state["evaluation"]["path"], "project", RULE_NOTE, output_dir=root / "cannot-adopt"))
        pub_state = task_action(public, "publish", review_note=RULE_NOTE)
        value = json.loads(Path(pub_state["publication"]["path"]).read_bytes())
        assert not value["all_cases_full_fix_verified"] and value["cases"][0]["receipt"] is None
        assert value["cases"][0]["public_retest"]["summary"]["failed"] == 0 and value["cases"][0]["independent_status"] == "not_provided"

        # 无关或过期 XML 只允许得到基线不匹配结果，模型请求为零。
        mismatch = root / "wrong.xml"
        mismatch.write_bytes(xml_raw.replace(b'test_started_block', b'test_wrong_identity'))
        wrong = root / "错报告任务"
        create_project_task(source, NOTE, output_dir=wrong, junit_report=mismatch, max_turns=4)
        model = fixed_model()
        failed = task_action(wrong, "diagnose", call_model=model)
        assert failed["status"] == "diagnosis_incomplete" and not model.calls
        assert json.loads(Path(failed["diagnosis"]["path"]).read_bytes())["rows"][0]["outcome"] == "baseline_mismatch"
        for target in (source / "metering.py", xml, task / "project-source/provided.xml", task / "project-source/test-config.json"):
            raw = target.read_bytes(); target.write_bytes(raw + b"\n")
            reject(lambda: task_action(task, "show")); target.write_bytes(raw)
        reject(lambda: create_project_task(source, NOTE, output_dir=source / "nested"))
        reject(lambda: create_project_task(source, NOTE, output_dir=root / "fake-private", regression_tests=source / "tests"))
        reject(lambda: create_project_task(source, NOTE, output_dir=root / "conflict", test_config=config, test_paths=["tests"]))
        reject(lambda: create_project_task(source, NOTE, output_dir=task))
        task_action(task, "resume")
        assert snapshot_sources(source)["files"] == before and xml.read_bytes() == xml_raw
        return {"status": "passed", "direct_external_unicode_project_and_junit_cli_without_suite_or_config": True,
            "parent_bound_diagnose_full_independent_verification_publication": True,
            "public_only_result_explicit_and_no_adoption_receipt": True, "mismatched_junit_stops_before_model_requests": True,
            "original_or_copied_source_xml_config_changes_rejected": True, "nested_output_fake_independent_and_option_conflicts_rejected": True,
            "external_source_and_xml_unchanged": True, "new_network_calls": 0}
