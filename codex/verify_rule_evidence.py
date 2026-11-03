"""版本化显式规则的真实本地闭环；--live 只收集模型诊断，候选另审。"""

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex.verify_delivery import message
from personal_agent.ci_diagnosis import load_checked_diagnosis, run_ci_diagnosis
from personal_agent.ci_repair import apply_repair, rollback_repair
from personal_agent.diagnosis_contract import RUN, FAILURES
from personal_agent.file_runtime import write_json_atomic
from personal_agent.repair_eval import load_run, run_evaluation, verify_evaluation
from personal_agent.repair_eval_gate import issue_evaluation_receipt
from personal_agent.repair_eval_suite import load_suite
from personal_agent.rule_evidence import RULES
from personal_agent.source_snapshot import PREFIX, digest, snapshot_sources

SUITE = ROOT / "examples/rule-evidence/suite.json"
NOTE = "已审阅自编本地项目/测试，允许基线执行，实际候选另审。"
RULE_NOTE = "明确提交并审阅自编 demo-rule-v1，只用于对应演示函数和指定对象；不是真实业务规则。"


def fixed_model(mode="correct"):
    calls = []
    def model(history, definitions, *, on_usage, response_schema=None, **_):
        calls.append(history)
        raw = json.dumps(history, ensure_ascii=False)
        assert "PRIVATE_RULE_EVIDENCE" not in raw and "evaluation_tests" not in raw
        on_usage({"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3})
        results = {item["tool_call_id"]: item["content"] for item in history if item["role"] == "tool"}
        def lines(identity):
            return [(int(match[1]), match[2]) for match in re.finditer(r"(?m)^\s*(\d+) \| (.*)$", results[identity])]
        if not results:
            return message(("run", "read_file", {"file_path": RUN}), ("failures", "read_file", {"file_path": FAILURES}))
        metadata = json.loads("\n".join(text for _, text in lines("run")))
        names = sorted(metadata["source_files"])
        if "file0" not in results:
            tools = [("file" + str(i), "read_file", {"file_path": PREFIX + name}) for i, name in enumerate(names)]
            if mode != "unread":
                tools.append(("rules", "read_file", {"file_path": RULES}))
            return message(*tools)
        if response_schema is None:
            return {"role": "assistant", "content": "已读失败、源码和显式规则；仅指定对象可新增规则分支，其他对象仍未知，尚未复验。"}
        module = next(name for name in names if "/" not in name)
        code = lines("file" + str(names.index(module)))
        # unread 是故意伪造引用的固定响应；不是模型质量测试。
        rule_end = len(lines("rules")) if "rules" in results else 14
        rule_cite = {"path": RULES, "start_line": 1, "end_line": rule_end if mode != "short_cite" else 1}
        failures, edits = [], []
        for number, text in lines("failures"):
            case = json.loads(text)
            local = case["name"] == "test_started_block"
            line, old = next((n, text) for n, text in code if ("return minutes //" in text if local else "raise LookupError" in text))
            cite = {"path": PREFIX + module, "start_line": line, "end_line": line}
            ids = [] if local or mode == "missing_id" else [metadata["rules"]["ids"][0]]
            if mode == "unknown_id" and not local:
                ids = ["invented-id"]
            failures.append({"test": case["test"], "status": "suspected", "hypothesis": "局部计算可修。" if local else "当前显式规则支持该对象的受限分支。",
                "recommendation": "最小修改后公开及独立复验。", "missing_information": [],
                "evidence": [{"path": FAILURES, "start_line": number, "end_line": number}, cite] + ([] if local else [rule_cite])})
            new = old.replace("minutes // block_size", "(minutes + block_size - 1) // block_size") if local else (
                '    if account == "regulated":\n        return 90\n' + old if module == "metering.py" else '    if employee == "employee-42":\n        return 0.21\n' + old)
            edits.append({"path": PREFIX + module, "old": old, "new": new, "addresses": [case["test"]], "rule_ids": ids,
                "evidence": [cite] + ([] if local else [rule_cite])})
        return {"role": "assistant", "content": json.dumps({"summary": "固定响应的显式规则候选，尚未复验。", "failures": failures, "edits": edits,
            "limitations": ["仅自编演示规则，不扩展为全局默认值。"]}, ensure_ascii=False)}
    model.calls = calls
    return model


def fixed_reviews(run, path):
    write_json_atomic(path, [{"case_id": row["case_id"], "proposal_sha256": row["proposal_sha256"],
        "note": "已审阅本固定脚本已知的 ceil 及指定对象分支，保留未知对象异常，允许独立复验。"} for row in run["rows"] if row.get("candidate")])


def reject(action):
    try:
        action()
    except (ValueError, OSError):
        return
    raise AssertionError("invalid or stale rule evidence accepted")


def fixture(root):
    shutil.copytree(SUITE.parent, root / "suite")
    run = run_evaluation(root / "suite/suite.json", NOTE, rules_review_note=RULE_NOTE, output_dir=root / "run", call_model=fixed_model(), max_turns=4)
    assert run["status"] == "awaiting_review", run
    fixed_reviews(run, root / "reviews.json")
    result = verify_evaluation(root / "run", root / "reviews.json", output_dir=root / "evaluation")
    assert all(row["outcome"] == "full_fix_verified" for row in result["rows"]), result
    return result


def verify_workflow():
    with tempfile.TemporaryDirectory(prefix="rule-evidence-中文 空格-") as temporary:
        root = Path(temporary).resolve()
        fixture(root)
        original = (root / "run/report.json").read_bytes()
        for name in ("dev-metering", "holdout-tax"):
            diagnosis = load_checked_diagnosis(root / "run/cases" / name / "diagnosis/report.json")
            assert diagnosis["rules_source"]["revision"] == "demo-rule-v1" and diagnosis["repair_scope"]["status"] == "proposal_for_all_failures"
        command = [sys.executable, "-X", "utf8", "-B", str(ROOT / "run_task.py"), "repair-eval", "receipt", "--evaluation-report", "evaluation/report.json", "--case", "dev-metering",
            "--review-note", RULE_NOTE, "--output-dir", "规则凭据"]
        process = subprocess.run(command, cwd=root, capture_output=True, timeout=30)
        assert process.returncode == 0, process.stderr.decode("utf-8", errors="replace")
        receipt = root / "规则凭据/receipt.json"
        source = root / "run/cases/dev-metering/project"
        before = snapshot_sources(source)["files"]
        state = apply_repair(receipt, RULE_NOTE, output_dir=root / "repair")
        try:
            assert state["status"] == "applied" and state["regression_gate"]["outcome"] == "full_fix_verified"
        finally:
            rollback_repair(root / "repair", "恢复规则演示原源码。")
        assert snapshot_sources(source)["files"] == before and (root / "run/report.json").read_bytes() == original
        load_run(root / "run")
        return {"status": "passed", "versioned_rules_actual_read_scope_citations_and_ids": True, "two_cases_public_and_independent_full_fix": True,
            "v4_explicit_adoption_and_byte_rollback": True, "unicode_external_cwd_cli_receipt": True, "new_network_calls": 0}


def verify_guards():
    with tempfile.TemporaryDirectory(prefix="rule-evidence-guards-") as temporary:
        root = Path(temporary).resolve()
        fixture(root)
        base = root / "run/cases/dev-metering"
        args = (base / "public-baseline/run.xml", base / "project")
        reject(lambda: run_ci_diagnosis(*args, rules_file=base / "rules.json", output_dir=root / "no-note", call_model=fixed_model()))
        reject(lambda: run_evaluation(root / "suite/suite.json", NOTE, output_dir=root / "no-suite-note", call_model=fixed_model()))
        for mode, expected in (("missing_id", "rule_ids_and_citations_match_changed_functions"), ("unknown_id", "diagnosis_schema"),
                ("short_cite", "rule_ids_and_citations_match_changed_functions"), ("unread", "rules_file_actually_read")):
            report = run_ci_diagnosis(*args, rules_file=base / "rules.json", rules_review_note=RULE_NOTE, output_dir=root / mode, call_model=fixed_model(mode), max_turns=4)
            assert report["status"] == "rejected" and expected in report["failure"]["failed_checks"], report.get("failure")
        rule = root / "suite/dev-metering/rules.json"
        raw = rule.read_bytes()
        malformed = json.loads(raw); malformed["rules"][0]["source_sha256"] = "0" * 64
        write_json_atomic(rule, malformed)
        model = fixed_model()
        reject(lambda: run_evaluation(root / "suite/suite.json", NOTE, rules_review_note=RULE_NOTE, output_dir=root / "stale-preflight", call_model=model))
        assert not model.calls and not (root / "stale-preflight").exists()
        rule.write_bytes(raw)
        issue_evaluation_receipt(root / "evaluation/report.json", "dev-metering", RULE_NOTE, output_dir=root / "good")
        for target in (rule, base / "rules.json", base / "diagnosis/rules-source.json", base / "diagnosis/workspace" / RULES):
            content = target.read_bytes(); target.write_bytes(content + b"\n")
            reject(lambda: apply_repair(root / "good/receipt.json", RULE_NOTE, output_dir=root / "stale"))
            assert not (root / "stale").exists()
            target.write_bytes(content)
        # 模型请求完成后、候选落盘前，原规则变化也必须拒绝。
        model = fixed_model()
        def changed(history, definitions, **options):
            value = model(history, definitions, **options)
            if options.get("response_schema") is not None:
                (base / "rules.json").write_bytes(raw + b"\n")
            return value
        report = run_ci_diagnosis(*args, rules_file=base / "rules.json", rules_review_note=RULE_NOTE, output_dir=root / "changed", call_model=changed, max_turns=4)
        assert report["failure"]["kind"] == "rules_changed" and not (root / "changed/candidate-source").exists()
        (base / "rules.json").write_bytes(raw)
        load_run(root / "run")
        def changed_suite(history, definitions, **options):
            value = fixed(history, definitions, **options)
            if options.get("response_schema") is not None:
                target = root / "changed-suite/cases/dev-metering/rules.json"
                target.write_bytes(raw + b"\n")
            return value
        fixed = fixed_model()
        stopped = run_evaluation(root / "suite/suite.json", NOTE, rules_review_note=RULE_NOTE, output_dir=root / "changed-suite", call_model=changed_suite, max_turns=4)
        assert stopped["status"] == "partial" and not stopped["inputs_unchanged"] and stopped["metrics"]["all"]["format"]["failed"] == 0
        return {"status": "passed", "explicit_rule_review_required_before_calls": True, "unread_unknown_or_missing_ids_and_short_citations_rejected": True,
            "stale_source_fingerprint_stops_before_calls": True, "original_frozen_raw_and_visible_rule_bytes_bound_to_adoption": True,
            "rules_changed_during_inference_no_candidate": True, "rule_source_change_not_miscounted_as_bad_model_json": True, "new_network_calls": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or ROOT / "codex/acceptance-runs" / ("rule-evidence-" + uuid4().hex + ".json")
    if output.exists():
        parser.error("输出已存在，未覆盖。")
    if args.live:
        suite = load_suite(SUITE)
        freeze_path = ROOT / "codex/rule-evidence-input-freeze-2026-10-31.json"
        frozen = json.loads(freeze_path.read_bytes())
        assert suite["sha256"] == frozen["suite_sha256"]
        assert [{"id": case["id"], "project": case["project_snapshot"]["manifest"], "regression": case["regression_snapshot"]["manifest"], "config": case["config_source"], "rules": case["rules_input"]["source"]} for case in suite["cases"]] == frozen["cases"]
        directory = ROOT / ".personal/rule-evidence-demos" / uuid4().hex
        run = run_evaluation(SUITE, NOTE, rules_review_note=RULE_NOTE, output_dir=directory / "run", max_turns=8)
        value = {"status": run["status"], "run": run, "freeze_sha256": digest(freeze_path.read_bytes()),
            "boundary": "real diagnostic replay with explicitly self-authored rules, not actual business authority, no automatic candidate execution"}
    else:
        value = {"status": "passed", "workflow": verify_workflow(), "guards": verify_guards()}
    write_json_atomic(output, value)
    print(json.dumps({"status": value["status"], "output": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
