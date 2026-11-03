"""证据不足/局部方案：固定模型、真实读取/pytest/采纳；--live 仅收集真实诊断，候选另审。"""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
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
from personal_agent.source_snapshot import PREFIX, digest, snapshot_sources

SUITE = ROOT / "examples/evidence-limits/suite.json"
NOTE = "已读取两个自编公开项目与独立测试；允许可信本地基线执行，候选另行审阅。"


def fixed_model(mode="correct"):
    def model(history, definitions, *, on_usage, response_schema=None, **_):
        raw = json.dumps(history, ensure_ascii=False)
        assert "PRIVATE_EVIDENCE_LIMITS" not in raw and "evaluation_tests" not in raw
        on_usage({"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3})
        results = {item["tool_call_id"]: item["content"] for item in history if item["role"] == "tool"}
        def lines(identity):
            return [(int(match[1]), match[2]) for match in re.finditer(r"(?m)^\s*(\d+) \| (.*)$", results[identity])]
        if not results:
            return message(("run", "read_file", {"file_path": RUN}), ("failures", "read_file", {"file_path": FAILURES}))
        names = sorted(json.loads("\n".join(text for _, text in lines("run")))["source_files"])
        if "file0" not in results:
            return message(*[("file" + str(i), "read_file", {"file_path": PREFIX + name}) for i, name in enumerate(names)])
        if response_schema is None:
            return {"role": "assistant", "content": "已读本例报告、实现、公开测试；局部计费块可修，权威外部规则缺失保持未知。"}
        module = next(name for name in names if "/" not in name)
        code = lines("file" + str(names.index(module)))
        failures, edits = [], []
        for number, text in lines("failures"):
            case = json.loads(text)
            known = case["name"] == "test_started_block"
            line, source = next((n, text.strip()) for n, text in code if ("return minutes //" in text if known else "raise LookupError" in text))
            cite = {"path": PREFIX + module, "start_line": line, "end_line": line}
            failures.append({"test": case["test"], "status": "suspected" if known or mode == "fabricated_supported" else "insufficient_evidence",
                "hypothesis": "started block 规则与 floor 计算不一致。" if known else "权威外部规则未提供，测试目标值不能作为配置依据。",
                "recommendation": "仅修复局部整数计算并复验。" if known else "请规则维护方提供适用主体、版本和有来源的权威规则，再决定实现。",
                "missing_information": [] if known or mode in {"fabricated_supported", "empty_missing"} else ["适用主体与版本对应的权威规则表及其来源。"],
                "evidence": [{"path": FAILURES, "start_line": number, "end_line": number}, cite] if known or mode != "no_source" else [{"path": FAILURES, "start_line": number, "end_line": number}]})
            if known:
                edits.append({"path": PREFIX + module, "old": source, "new": "return (minutes + block_size - 1) // block_size",
                    "addresses": [case["test"]], "evidence": [cite]})
            elif mode in {"contradiction", "indirect", "fabricated_supported"}:
                edits.append({"path": PREFIX + module, "old": source, "new": "return 90" if module == "metering.py" else "return 0.21",
                    "addresses": [failures[0]["test"]] if mode == "indirect" else [case["test"]], "evidence": [cite]})
        return {"role": "assistant", "content": json.dumps({"summary": "固定响应，只验证声明约束与局部方案。", "failures": failures,
            "edits": edits, "limitations": ["外部规则仍缺失，未执行测试或采纳。"]}, ensure_ascii=False)}
    return model


def fixed_reviews(run, path):
    values = []
    for row in run["rows"]:
        if row.get("candidate"):
            values.append({"case_id": row["case_id"], "proposal_sha256": row["proposal_sha256"],
                "note": "本固定响应脚本的简单计算/常量候选已知且已审阅，仅允许受限副本测量，编造常量预期应回归。"})
    write_json_atomic(path, values)


def reject(action):
    try:
        action()
    except (ValueError, OSError):
        return
    raise AssertionError("invalid scope or regression accepted")


def verify_workflow():
    with tempfile.TemporaryDirectory(prefix="evidence-limits-中文 空格-") as temporary:
        root = Path(temporary).resolve()
        suite = load_suite(SUITE)
        before = [case["project_snapshot"]["manifest"] for case in suite["cases"]]
        run = run_evaluation(SUITE, NOTE, output_dir=root / "run", call_model=fixed_model(), max_turns=4)
        assert run["metrics"]["all"]["declared_insufficient_failures"] == 2
        for name, expected in (("dev-metering", "partial_proposal"), ("holdout-tax", "no_candidate")):
            report = load_checked_diagnosis(root / "run/cases" / name / "diagnosis/report.json")
            assert report["decision_contract_version"] == 2 and report["repair_scope"]["status"] == expected
            assert report["repair_scope"]["unresolved"][0]["missing_information"]
            assert "待补充的依据" in (Path(report["directory"]) / "report.md").read_text(encoding="utf-8")
        fixed_reviews(run, root / "reviews.json")
        measured = verify_evaluation(root / "run", root / "reviews.json", output_dir=root / "evaluation")
        assert {row["case_id"]: row["outcome"] for row in measured["rows"]} == {"dev-metering": "partial_fix_verified", "holdout-tax": "no_candidate"}
        command = [sys.executable, "-X", "utf8", "-B", str(ROOT / "run_task.py"), "repair-eval", "receipt", "--evaluation-report", "evaluation/report.json",
            "--case", "dev-metering", "--review-note", "本固定响应只改块计数并保留缺失规则，明确部分方案。", "--output-dir", "部分凭据"]
        process = subprocess.run(command, cwd=root, capture_output=True, timeout=30)
        assert process.returncode == 0, process.stderr.decode("utf-8", errors="replace")
        receipt = root / "部分凭据/receipt.json"
        reject(lambda: apply_repair(receipt, NOTE, output_dir=root / "unconfirmed"))
        source = root / "run/cases/dev-metering/project"
        raw = (source / "metering.py").read_bytes()
        state = apply_repair(receipt, NOTE, output_dir=root / "repair", accept_partial=True)
        try:
            assert state["status"] == "applied_partial"
            current = (source / "metering.py").read_text(encoding="utf-8")
            assert "return (minutes + block_size - 1) // block_size" in current and "raise LookupError" in current
        finally:
            rollback_repair(root / "repair", "恢复自编冻结项目。")
        assert (source / "metering.py").read_bytes() == raw
        load_run(root / "run")
        assert before == [case["project_snapshot"]["manifest"] for case in load_suite(SUITE)["cases"]]
        return {"status": "passed", "partial_and_no_candidate_with_concrete_missing_inputs": True,
            "same_file_known_function_fixed_unknown_function_preserved": True, "independent_regression_and_v4_partial_apply_rollback": True,
            "external_unicode_cwd_cli_receipt": True, "private_tests_absent_from_fixed_requests": True, "new_network_calls": 0}


def verify_guards():
    with tempfile.TemporaryDirectory(prefix="evidence-limits-guards-") as temporary:
        root = Path(temporary).resolve()
        run_evaluation(SUITE, NOTE, output_dir=root / "reference", call_model=fixed_model(), max_turns=4)
        base = root / "reference/cases/dev-metering"
        failures = {}
        for mode, expected in (("contradiction", "edits_address_supported_failures_only"), ("indirect", "insufficient_evidence_code_preserved"),
                ("empty_missing", "diagnosis_schema"), ("no_source", "insufficient_evidence_code_preserved")):
            report = run_ci_diagnosis(base / "public-baseline/run.xml", base / "project", call_model=fixed_model(mode), output_dir=root / mode, max_turns=4)
            assert report["status"] == "rejected" and expected in report["failure"]["failed_checks"], report.get("failure")
            assert not (root / mode / "candidate-source").exists()
            failures[mode] = expected
        good = base / "diagnosis/report.json"
        raw = good.read_bytes(); changed = json.loads(raw); changed["repair_scope"]["status"] = "proposal_for_all_failures"
        write_json_atomic(good, changed)
        reject(lambda: load_checked_diagnosis(good)); good.write_bytes(raw)
        # 声明自己“有依据”仍可能说错；独立测试负责保留第二道门槛，不能冒称语义核验。
        forged = run_evaluation(SUITE, NOTE, output_dir=root / "fabricated", call_model=fixed_model("fabricated_supported"), max_turns=4)
        assert forged["metrics"]["all"]["evidence"]["passed"] == 2
        fixed_reviews(forged, root / "bad-reviews.json")
        measured = verify_evaluation(root / "fabricated", root / "bad-reviews.json", output_dir=root / "bad-evaluation")
        assert all(row["outcome"] == "regression_detected" for row in measured["rows"])
        reject(lambda: issue_evaluation_receipt(root / "bad-evaluation/report.json", "dev-metering", NOTE, output_dir=root / "bad-receipt"))
        return {"status": "passed", "contradictions_and_missing_input_shape_rejected": failures,
            "scope_summary_tampering_rejected": True, "false_supported_claim_still_requires_independent_regression": True, "new_network_calls": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or ROOT / "codex/acceptance-runs" / ("evidence-limits-" + uuid4().hex + ".json")
    if output.exists():
        parser.error("输出已存在，未覆盖。")
    if args.live:
        suite = load_suite(SUITE)
        frozen = json.loads((ROOT / "codex/evidence-limits-input-freeze-2026-10-31.json").read_text(encoding="utf-8"))
        assert suite["sha256"] == frozen["suite_sha256"]
        assert [{"id": case["id"], "split": case["split"], "project": case["project_snapshot"]["manifest"], "regression": case["regression_snapshot"]["manifest"],
            "test_config": case["config_source"]} for case in suite["cases"]] == frozen["cases"]
        directory = ROOT / ".personal/evidence-limits-demos" / uuid4().hex
        run = run_evaluation(SUITE, NOTE, output_dir=directory / "run", max_turns=8)
        result = {"status": run["status"], "run": run, "freeze_sha256": digest((ROOT / "codex/evidence-limits-input-freeze-2026-10-31.json").read_bytes()),
            "boundary": "one real diagnosis per new self-authored case; no automatic review, retest or adoption"}
    else:
        result = {"status": "passed", "workflow": verify_workflow(), "guards": verify_guards()}
    write_json_atomic(output, result)
    print(json.dumps({"status": result["status"], "output": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
