"""独立回归采纳门槛：固定诊断、真实复验/文件/CLI；不请求外部模型。"""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex.verify_repair_eval import NOTE, SUITE, fixed_model, reviews_for
from personal_agent import ci_repair
from personal_agent.configured_tests import run_project_tests
from personal_agent.configured_verification import verify_configured_proposal
from personal_agent.file_runtime import write_json_atomic
from personal_agent.repair_eval import load_run, run_evaluation, verify_evaluation
from personal_agent.repair_eval_gate import issue_evaluation_receipt
from personal_agent.repair_verification import load_checked_verification
from personal_agent.source_snapshot import snapshot_sources


def fixture(root, *, regress=False):
    run = run_evaluation(SUITE, NOTE, output_dir=root / "run", call_model=fixed_model(regress=regress), max_turns=4)
    reviews_for(run, root / "reviews.json")
    result = verify_evaluation(root / "run", root / "reviews.json", output_dir=root / "evaluation")
    assert result["status"] == "evaluated"
    return result


def cli(root, arguments):
    process = subprocess.run([sys.executable, "-X", "utf8", "-B", str(ROOT / "run_task.py"), *arguments],
        cwd=root, capture_output=True, timeout=60)
    return process.returncode, json.loads(process.stdout) if process.stdout else None, process.stderr.decode("utf-8", errors="replace")


def reject(action):
    try:
        action()
    except (ValueError, OSError):
        return
    raise AssertionError("regression or stale evidence accepted")


def verify_workflow():
    with tempfile.TemporaryDirectory(prefix="eval-adoption-中文 空格-") as temporary:
        root = Path(temporary).resolve()
        result = fixture(root)
        path = root / "evaluation/report.json"
        raw_run, raw_result = (root / "run/report.json").read_bytes(), path.read_bytes()
        before = {name: snapshot_sources(root / "run/cases" / name / "project")["files"] for name in ("dev-01", "dev-02", "holdout-01")}
        code, issued, error = cli(root, ["repair-eval", "receipt", "--evaluation-report", "evaluation/report.json", "--case", "dev-01",
            "--review-note", "重读本固定候选的公开与独立回归证据，生成采纳凭据。", "--output-dir", "完整回执"])
        assert code == 0 and issued["status"] == "verified_with_regression", error
        receipt_a = root / "完整回执/receipt.json"
        assert ci_repair.apply_repair(receipt_a, "明确采纳已绑定回归的标签改动。", output_dir=root / "repair-a")["status"] == "applied"
        # 另一个独立案例已经改变，不应使当前案例的证据失效。
        issue_evaluation_receipt(path, "holdout-01", "分页案例独立测试已通过，选择本例。", output_dir=root / "receipt-b")
        assert ci_repair.apply_repair(root / "receipt-b/receipt.json", "明确采纳分页改动。", output_dir=root / "repair-b")["status"] == "applied"
        ci_repair.rollback_repair(root / "repair-a", "恢复标签案例原字节。")
        ci_repair.rollback_repair(root / "repair-b", "恢复分页案例原字节。")
        value = issue_evaluation_receipt(path, "dev-02", "仅金额修复，无新增回归；远程数据仍缺失。", output_dir=root / "partial-receipt")
        assert value["status"] == "partial_fix_verified_with_regression" and not value["all_failures_fixed"]
        reject(lambda: ci_repair.apply_repair(root / "partial-receipt/receipt.json", NOTE, output_dir=root / "not-confirmed"))
        assert not (root / "not-confirmed").exists()
        code, applied, error = cli(root, ["repair", "apply", "--verification", "partial-receipt/receipt.json", "--review-note", "明确接受远程配置仍未解决的部分修复。",
            "--accept-partial", "--output-dir", "部分采纳"])
        assert code == 0 and applied["status"] == "applied_partial", error
        assert applied["regression_gate"]["outcome"] == "partial_fix_verified"
        source = root / "run/cases/dev-02/project"
        tested = run_project_tests(source, root / "run/cases/dev-02/test-config.json", NOTE, output_dir=root / "post-apply")
        assert tested["status"] == "tests_failed" and tested["summary"]["failed"] == 1
        ci_repair.rollback_repair(root / "部分采纳", "恢复金额案例原字节。")
        for name, files in before.items():
            assert snapshot_sources(root / "run/cases" / name / "project")["files"] == files
        assert raw_run == (root / "run/report.json").read_bytes() and raw_result == path.read_bytes()
        load_run(root / "run")
        return {"status": "passed", "v4_issue_apply_and_byte_rollback": True,
            "two_independent_cases_can_be_adopted_without_rolling_back_first": True,
            "partial_requires_explicit_flag_and_keeps_unresolved_failure": True,
            "external_unicode_cwd_cli_receipt_and_apply": True, "historical_reports_unchanged": True, "new_network_calls": 0}


def verify_guards():
    with tempfile.TemporaryDirectory(prefix="eval-adoption-guards-") as temporary:
        root = Path(temporary).resolve()
        fixture(root, regress=True)
        path = root / "evaluation/report.json"
        public = root / "evaluation/holdout-01/public/receipt.json"
        source = root / "run/cases/holdout-01/project"
        before = snapshot_sources(source)["files"]
        reject(lambda: issue_evaluation_receipt(path, "holdout-01", NOTE, output_dir=root / "bad"))
        reject(lambda: issue_evaluation_receipt(path, "holdout-02", NOTE, output_dir=root / "none"))
        reject(lambda: ci_repair.apply_repair(public, NOTE, output_dir=root / "public-only", accept_partial=True))
        assert not (root / "public-only").exists()
        fresh = verify_configured_proposal(root / "run/cases/holdout-01/diagnosis/report.json", root / "run/cases/holdout-01/test-config.json", NOTE,
            output_dir=root / "fresh-public")
        assert fresh["status"] == "verified_with_test_config"
        reject(lambda: ci_repair.apply_repair(root / "fresh-public/receipt.json", NOTE, output_dir=root / "fresh-bypass"))
        # 删除新 marker 模拟第 9 步旧案例，原输入锁仍必须强制独立门槛。
        marker = root / "run/cases/holdout-01/evaluation-origin.json"
        marker_raw = marker.read_bytes(); marker.unlink()
        reject(lambda: ci_repair.apply_repair(public, NOTE, output_dir=root / "legacy-bypass"))
        marker.write_bytes(marker_raw)
        assert snapshot_sources(source)["files"] == before
        raw = path.read_bytes(); value = json.loads(raw)
        bad_row = next(row for row in value["rows"] if row["case_id"] == "holdout-01")
        bad_row.update(outcome="full_fix_verified", regression_clean=True)
        bad_row["regression"]["new_failures"] = []
        write_json_atomic(path, value)
        reject(lambda: issue_evaluation_receipt(path, "holdout-01", NOTE, output_dir=root / "forged-pass"))
        path.write_bytes(raw)
        issue_evaluation_receipt(path, "dev-01", NOTE, output_dir=root / "good")
        receipt = root / "good/receipt.json"
        targets = [root / "evaluation/dev-01/candidate-regression/run.xml", root / "evaluation/dev-01/candidate-regression/run-runtime/pytest.log",
            root / "evaluation/dev-01/candidate-regression/source/evaluation_tests/test_regression.py",
            root / "run/cases/dev-01/regression/test_regression.py", root / "run/cases/dev-01/test-config.json", root / "reviews.json"]
        for target in targets:
            content = target.read_bytes(); target.write_bytes(content + b"\n")
            reject(lambda: ci_repair.apply_repair(receipt, NOTE, output_dir=root / "stale"))
            assert not (root / "stale").exists()
            target.write_bytes(content)
        content = receipt.read_bytes(); forged = json.loads(content); forged["case_id"] = "dev-02"
        write_json_atomic(receipt, forged)
        reject(lambda: load_checked_verification(receipt))
        receipt.write_bytes(content)
        # 准备备份后到真正替换前，证据变化仍必须拦截，并恢复/保持源字节。
        target = root / "evaluation/dev-01/candidate-regression/run-runtime/pytest.log"
        log = target.read_bytes(); save = ci_repair._save
        def change_after_prepared(directory, state, status, **extra):
            save(directory, state, status, **extra)
            if status == "prepared":
                target.write_bytes(log + b"\n")
        good_source = root / "run/cases/dev-01/project"
        original = snapshot_sources(good_source)["files"]
        with patch.object(ci_repair, "_save", change_after_prepared):
            result = ci_repair.apply_repair(receipt, NOTE, output_dir=root / "changed-after-lock")
        assert result["status"] == "apply_failed_restored" and snapshot_sources(good_source)["files"] == original
        target.write_bytes(log)
        load_checked_verification(receipt)
        return {"status": "passed", "regression_no_candidate_and_forged_pass_rejected": True,
            "public_only_fresh_public_and_legacy_case_bypasses_blocked": True,
            "xml_log_execution_copy_private_tests_config_review_and_case_id_bound": True,
            "final_locked_recheck_preserves_source_on_late_evidence_change": True, "new_network_calls": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or ROOT / "codex/acceptance-runs" / f"eval-adoption-{uuid4().hex}.json"
    if output.exists():
        parser.error("输出已存在，未覆盖。")
    result = {"workflow": verify_workflow(), "guards": verify_guards(), "status": "passed"}
    write_json_atomic(output, result)
    print(json.dumps({"status": "passed", "output": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
