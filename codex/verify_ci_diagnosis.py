"""源码诊断/候选差异的固定与真实验证；真实候选需审阅后另行 --check-report 复验。"""

import argparse
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex.verify_delivery import isolated, message
from codex.verify_junit_ci import code_hashes, run_pytest
from personal_agent.ci_diagnosis import load_checked_diagnosis, recover_saved_diagnosis, run_ci_diagnosis
from personal_agent.diagnosis_contract import RUN, FAILURES
from personal_agent.file_runtime import write_json_atomic
from personal_agent.repair_verification import verify_demo_proposal
from personal_agent.source_snapshot import PREFIX
from personal_agent.usage import TokenPrices


def fixed_model(mode="correct"):
    calls = []
    def model(messages, definitions, *, on_usage, **_):
        calls.append(deepcopy(messages))
        assert {item["name"] for item in definitions} == {"read_file", "list_files", "grep_search"}
        on_usage({"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3})
        if len(messages) == 2:
            return message(("run", "read_file", {"file_path": RUN}), ("failures", "read_file", {"file_path": FAILURES}),
                ("list", "list_files", {"pattern": "*.py", "path": "playground/source"}))
        results = {item["tool_call_id"]: item["content"] for item in messages if item["role"] == "tool"}
        if mode == "forbidden" and "blocked" not in results:
            return message(("blocked", "write_file", {"file_path": PREFIX + "checkout.py", "content": "must not write"}))
        if "code" not in results:
            return message(("code", "read_file", {"file_path": PREFIX + "checkout.py"}),
                ("tests", "read_file", {"file_path": PREFIX + "demo_cases.py"}),
                ("search", "grep_search", {"pattern": "discounted_total|gateway_config", "path": "playground/source", "include": "*.py"}))
        if mode == "cancel":
            raise KeyboardInterrupt()
        import re
        def lines(identity):
            return [(int(match[1]), match[2]) for match in re.finditer(r"(?m)^\s*(\d+) \| (.*)$", results[identity])]
        cases = [json.loads(text) for _, text in lines("failures")]
        code_line, old_code = next((line, text.strip()) for line, text in lines("code") if "return round" in text)
        fixture_line, old_fixture = next((line, text.strip()) for line, text in lines("tests") if "raise RuntimeError" in text)
        assertion_line = next(line for line, text in lines("tests") if 'assert gateway_config["mode"]' in text)
        failures, edits = [], []
        for index, case in enumerate(cases, 1):
            is_code = case["name"] == "test_twenty_percent_discount"
            source = PREFIX + ("checkout.py" if is_code else "demo_cases.py")
            line = code_line if is_code else fixture_line
            evidence = [{"path": FAILURES, "start_line": index, "end_line": index},
                {"path": source, "start_line": line, "end_line": line}]
            if not is_code:
                evidence.append({"path": source, "start_line": assertion_line, "end_line": assertion_line})
            failures.append({"test": case["test"], "status": "suspected",
                "hypothesis": "疑似百分数与比例混用。" if is_code else "疑似 fixture 未返回测试所需配置。",
                "recommendation": "按读取的实现和测试做最小修改，再独立复验。", "evidence": evidence})
            old = old_code if is_code else old_fixture
            new = old_code.replace("1 - discount_percent", "1 - discount_percent / 100") if is_code else 'return {"mode": "sandbox"}'
            edits.append({"path": source, "old": old, "new": new, "addresses": [case["test"]], "evidence": evidence[1:]})
        if mode == "unread":
            failures[0]["evidence"].append({"path": PREFIX + "unread.py", "start_line": 1, "end_line": 1})
        if mode == "bad_line":
            failures[0]["evidence"][1]["start_line"] = 999
            failures[0]["evidence"][1]["end_line"] = 999
        if mode == "tests_changed":
            edits[0] = {"path": PREFIX + "demo_cases.py", "old": "== 80", "new": "== -1900", "addresses": [cases[0]["test"]],
                "evidence": [{"path": PREFIX + "demo_cases.py", "start_line": 13, "end_line": 13}]}
        if mode == "partial":
            edits = edits[:1]
            failures[1]["status"] = "insufficient_evidence"
        for item in failures:
            item["missing_information"] = ["需项目负责人提供权威 fixture 配置及来源。"] if item["status"] == "insufficient_evidence" else []
        content = json.dumps({"summary": "根据实际失败和源码形成待验证方案。", "failures": failures,
            "edits": edits, "limitations": ["尚未执行测试复验，未改原项目。"]}, ensure_ascii=False)
        return {"role": "assistant", "content": "```json\n" + content + "\n```" if mode == "fenced" else content}
    model.calls = calls
    return model


def setup(root):
    baseline = run_pytest(root / "broken-project", root / "junit/broken.xml")
    return baseline


def flow(root, *, live=False):
    baseline = setup(root)
    before = code_hashes(root / "broken-project")
    with isolated(root), patch("personal_agent.task_runner.ROOT", root):
        result = run_ci_diagnosis(baseline["xml"], root / "broken-project", output_mode="json_schema" if live else "legacy", call_model=None if live else fixed_model())
        assert result["status"] == "evidence_checked" and result["diagnosis"]["edits"]
        assert code_hashes(root / "broken-project") == before
        report_path = Path(result["directory"]) / "report.json"
        load_checked_diagnosis(report_path)
        verification = None if live else verify_demo_proposal(report_path, "固定响应含两处已知最小修改，作为程序契约测试进行复验。")
        if verification is not None:
            assert verification["status"] == "verified_on_demo"
        return {"status": "passed", "validation": "real diagnosis with actual read tools; retest pending separate review" if live else "fixed diagnosis; actual pytest/read tools/isolated retest",
            "project": str(root), "baseline": baseline, "diagnosis": result, "verification": verification,
            "source_unchanged": True, "boundary": "no original source deployment; live candidate must be reviewed before separate fixture-only pytest verification"}


def verify_workflow():
    with tempfile.TemporaryDirectory(prefix="ci-diagnosis-") as temporary:
        result = flow(Path(temporary).resolve())
        return {"status": "passed", "read_only_model_tools": True, "source_quotes_and_unique_edits_checked": True,
            "original_source_unchanged": True, "isolated_pytest_after_fixed_proposal": result["verification"]["summary"],
            "fixed_logical_calls": result["diagnosis"]["usage"]["calls"]}


def rejected(action):
    try:
        action()
    except ValueError:
        return
    raise AssertionError("changed or unsupported record accepted")


def verify_guards():
    with tempfile.TemporaryDirectory(prefix="ci-diagnosis-guards-") as temporary:
        root = Path(temporary).resolve()
        baseline = setup(root)
        source = root / "broken-project"
        with isolated(root), patch("personal_agent.task_runner.ROOT", root):
            for mode in ("forbidden", "bad_line", "tests_changed"):
                result = run_ci_diagnosis(baseline["xml"], source, output_mode="legacy", call_model=fixed_model(mode))
                assert result["status"] == "rejected" and not (Path(result["directory"]) / "candidate-source").exists()
            unread = source / "unread.py"
            unread.write_text("value = 1\n", encoding="utf-8")
            result = run_ci_diagnosis(baseline["xml"], source, output_mode="legacy", call_model=fixed_model("unread"))
            assert result["status"] == "rejected" and any(check["kind"] == "citations_exist_and_were_read" and not check["passed"] for check in result["checks"])
            unread.unlink()
            model = fixed_model()
            zero = run_ci_diagnosis(baseline["xml"], source, output_mode="legacy", call_model=model, prices=TokenPrices(Decimal(1), Decimal(1)), max_cost=Decimal(0))
            assert zero["status"] == "partial" and not model.calls and zero["usage"]["calls"] == 0
            cancelled = run_ci_diagnosis(baseline["xml"], source, output_mode="legacy", call_model=fixed_model("cancel"))
            assert cancelled["status"] == "cancelled" and cancelled["trace"] and not (Path(cancelled["directory"]) / "candidate-source").exists()
            framed = run_ci_diagnosis(baseline["xml"], source, output_mode="legacy", call_model=fixed_model("fenced"))
            assert framed["status"] == "evidence_checked" and framed["response_format"] == "single_json_fence"
            legacy = root / "legacy-record"
            legacy.mkdir()
            origin = Path(framed["directory"])
            shutil.copytree(origin / "workspace", legacy / "workspace")
            for name in ("source.xml", "model-response.txt"):
                shutil.copyfile(origin / name, legacy / name)
            old = deepcopy(framed)
            old.update(directory=str(legacy), status="partial", checks=[], artifacts={}, error={"type": "JSONDecodeError"})
            old.pop("diagnosis"); old.pop("resolved_evidence")
            write_json_atomic(legacy / "report.json", old)
            old_raw = (legacy / "report.json").read_bytes()
            recovered = recover_saved_diagnosis(legacy / "report.json")
            assert recovered["status"] == "evidence_checked" and recovered["recovery"]["new_model_calls"] == 0
            assert (legacy / "report.json").read_bytes() == old_raw
            load_checked_diagnosis(legacy / "recovered-report.json")
            partial = run_ci_diagnosis(baseline["xml"], source, output_mode="legacy", call_model=fixed_model("partial"))
            receipt = verify_demo_proposal(Path(partial["directory"]) / "report.json", "固定部分方案仅声明折扣改动，核对未处理的 fixture 错误保持。")
            assert receipt["status"] == "partial_fix_verified_on_demo" and not receipt["all_failures_fixed"] and receipt["pytest"]["returncode"] == 1
            good = run_ci_diagnosis(baseline["xml"], source, output_mode="legacy", call_model=fixed_model())
            path = Path(good["directory"]) / "report.json"
            candidate = Path(good["directory"]) / "candidate-source/checkout.py"
            raw = candidate.read_bytes()
            candidate.write_bytes(raw + b"# changed\n")
            rejected(lambda: load_checked_diagnosis(path))
            candidate.write_bytes(raw)
            extra = candidate.parent / "conftest.py"
            extra.write_text("raise RuntimeError('unregistered file must not execute')\n", encoding="utf-8")
            rejected(lambda: load_checked_diagnosis(path))
            extra.unlink()
            original = (source / "checkout.py").read_bytes()
            (source / "checkout.py").write_bytes(original + b"# changed\n")
            rejected(lambda: load_checked_diagnosis(path))
            (source / "checkout.py").write_bytes(original)
            empty = root / "empty.xml"
            empty.write_text("<testsuite/>", encoding="utf-8")
            no_failures = run_ci_diagnosis(empty, source, output_mode="legacy", call_model=model)
            assert no_failures["status"] == "no_failures" and no_failures["usage"]["calls"] == 0
            rejected(lambda: run_ci_diagnosis(baseline["xml"], source, output_dir=good["directory"], output_mode="legacy", call_model=model))
            return {"status": "passed", "write_tool_unread_or_invalid_citations_and_test_changes_rejected": True,
                "budget_zero_calls_cancel_no_candidate_and_no_failures_verified": True,
                "source_and_candidate_tampering_blocks_retest": True, "existing_output_preserved": True,
                "single_fence_and_zero_request_recovery_preserve_original_failure": True}


def entrypoint():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--check-report", type=Path)
    parser.add_argument("--recover-report", type=Path)
    parser.add_argument("--review-note")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or ROOT / "codex/acceptance-runs" / f"ci-diagnosis-{uuid4().hex}.json"
    if output.exists():
        parser.error("报告已存在，未执行或覆盖。")
    output.parent.mkdir(parents=True, exist_ok=True)
    if args.recover_report:
        if args.live or args.check_report:
            parser.error("本地恢复不与新诊断或复验合并。")
        recovered = recover_saved_diagnosis(args.recover_report)
        report = {"status": "passed" if recovered["status"] == "evidence_checked" else "failed", "recovery": recovered}
    elif args.check_report:
        if args.live:
            parser.error("审阅复验不与新模型诊断合并。")
        try:
            receipt = verify_demo_proposal(args.check_report, args.review_note)
        except (ValueError, OSError) as error:
            parser.error(str(error))
        report = {"status": "passed" if receipt["status"] in {"verified_on_demo", "partial_fix_verified_on_demo"} else "failed", "verification": receipt}
    else:
        report = {"workflow": verify_workflow(), "guards": verify_guards()}
        if args.live:
            project = ROOT / ".personal/diagnosis-demos" / uuid4().hex
            project.mkdir(parents=True, exist_ok=False)
            try:
                report["live"] = flow(project, live=True)
            except Exception as error:
                report["live"] = {"status": "failed", "project": str(project), "error": type(error).__name__, "message": str(error)}
        report["status"] = "passed" if all(value["status"] == "passed" for value in report.values()) else "failed"
    write_json_atomic(output, report)
    print(json.dumps({"status": report["status"], "output": str(output),
        "diagnosis": report.get("live", {}).get("diagnosis", {}).get("directory")}, ensure_ascii=False))
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    entrypoint()
