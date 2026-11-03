"""多故障评估的固定响应/真实文件与 pytest 验证；不自动运行真实模型候选。"""

import argparse
from copy import deepcopy
from decimal import Decimal
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
from personal_agent.diagnosis_contract import RUN, FAILURES
from personal_agent.file_runtime import write_json_atomic
from personal_agent.repair_eval import load_run, run_evaluation, verify_evaluation
from personal_agent.repair_eval_suite import load_suite
from personal_agent.source_snapshot import PREFIX, snapshot_sources
from personal_agent.usage import TokenPrices


SUITE = ROOT / "examples/repair-eval/suite.json"
NOTE = "已审阅四份自编公开项目与独立回归测试，允许在受限副本执行基线；候选另行按差异审阅，不采纳。"


def fixed_model(*, regress=False, malformed=False):
    calls = []
    def model(history, definitions, *, on_usage, response_schema=None, **_):
        raw = json.dumps(history, ensure_ascii=False)
        assert "PRIVATE_REGRESSION_ONLY:" not in raw and "evaluation_tests" not in raw
        calls.append(deepcopy(history))
        on_usage({"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3})
        results = {item["tool_call_id"]: item["content"] for item in history if item["role"] == "tool"}
        def lines(identity):
            return [(int(match[1]), match[2]) for match in re.finditer(r"(?m)^\s*(\d+) \| (.*)$", results[identity])]
        if not results:
            return message(("run", "read_file", {"file_path": RUN}), ("failures", "read_file", {"file_path": FAILURES}))
        metadata = json.loads("\n".join(text for _, text in lines("run")))
        names = sorted(metadata["source_files"])
        if "file0" not in results:
            return message(*[("file" + str(i), "read_file", {"file_path": PREFIX + name}) for i, name in enumerate(names)])
        if response_schema is None:
            return {"role": "assistant", "content": "已读取当前公开测试和实现；有数据依据的局部错误可给出最小改动，缺失外部信息保持未知。"}
        assert not definitions
        module = next(name for name in names if "/" not in name)
        code = lines("file" + str(names.index(module)))
        failures, edits = [], []
        for number, text in lines("failures"):
            case = json.loads(text)
            missing = module == "inventory.py" or module == "pricing.py" and case["name"] != "test_line_total"
            old = next(text.strip() for _, text in code if ("raise RuntimeError" in text if missing else
                "return text.lower()" in text if module == "tags.py" else "return round(price + quantity, 2)" in text if module == "pricing.py" else "return len(items) // page_size" in text))
            line = next(n for n, text in code if old in text)
            cite = {"path": PREFIX + module, "start_line": line, "end_line": line}
            failures.append({"test": case["test"], "status": "insufficient_evidence" if missing else "suspected",
                "missing_information": ["需配置维护方提供有来源的外部费率或部署清单。"] if missing else [],
                "hypothesis": "权威外部数据未提供，无法确定正确值。" if missing else "公开输入、断言和实现支持一个最小局部修复。",
                "recommendation": "请求权威配置，保留显式错误。" if missing else "仅修改函数体并独立复验。",
                "evidence": [{"path": FAILURES, "start_line": number, "end_line": number}, cite]})
            if not missing:
                new = ("return text.strip().lower()" if module == "tags.py" else "return round(price * quantity, 2)" if module == "pricing.py" else
                    "return len(items) // page_size + 1" if regress else "return (len(items) + page_size - 1) // page_size")
                edits.append({"path": PREFIX + module, "old": old, "new": new, "addresses": [case["test"]], "evidence": [cite]})
        content = json.dumps({"summary": "固定响应的评估契约，不是模型效果。", "failures": failures, "edits": edits,
            "limitations": ["尚未复验，缺失外部数据不编造。"]}, ensure_ascii=False)
        return {"role": "assistant", "content": "解释\n" + content if malformed and module == "tags.py" else content}
    model.calls = calls
    return model


def reviews_for(run, output):
    # 仅本固定响应测试使用；三个候选是本脚本已审阅的简单字面改动。
    values = [{"case_id": row["case_id"], "proposal_sha256": row["proposal_sha256"],
        "note": "已审阅本固定响应生成的预期函数体差异与全部源文件；执行独立评估，不采纳。"} for row in run["rows"] if row.get("candidate")]
    write_json_atomic(output, values)
    return values


def verify_workflow():
    with tempfile.TemporaryDirectory(prefix="repair-eval-中文 空格-") as temporary:
        root = Path(temporary).resolve()
        suite = load_suite(SUITE)
        original = [case["project_snapshot"]["manifest"] for case in suite["cases"]]
        model = fixed_model(regress=True)
        report = run_evaluation(SUITE, NOTE, output_dir=root / "run", call_model=model, max_turns=4)
        assert report["status"] == "awaiting_review" and report["usage"]["calls"] == 16
        assert report["metrics"]["all"]["evidence"]["passed"] == 4 and report["metrics"]["all"]["candidates"] == 3
        assert report["metrics"]["holdout"]["selected_cases"] == 2
        assert not list((root / "run").rglob("candidate-regression"))
        reviews_for(report, root / "审阅.json")
        # 实际跨进程 verify CLI，不请求模型；路径相对于外部中文空格 cwd。
        command = [sys.executable, "-X", "utf8", "-B", str(ROOT / "run_task.py"), "repair-eval", "verify",
            "--run-dir", "run", "--reviews", "审阅.json", "--output-dir", "评估结果"]
        process = subprocess.run(command, cwd=root, capture_output=True, timeout=60)
        assert process.returncode == 0, process.stderr.decode("utf-8", errors="replace")
        result = json.loads((root / "评估结果/report.json").read_text(encoding="utf-8"))
        assert result["status"] == "evaluated"
        outcomes = {row["case_id"]: row["outcome"] for row in result["rows"]}
        assert outcomes == {"dev-01": "full_fix_verified", "dev-02": "partial_fix_verified", "holdout-01": "regression_detected", "holdout-02": "no_candidate"}, outcomes
        bad = next(row for row in result["rows"] if row["case_id"] == "holdout-01")
        assert bad["public_retest"]["status"] == "verified_with_test_config" and len(bad["regression"]["new_failures"]) == 2
        assert result["metrics"]["all"]["regression_clean"] == {"passed": 2, "failed": 1, "not_observed": 1}
        assert result["new_model_calls"] == 0
        load_run(root / "run")
        current = load_suite(SUITE)
        assert original == [case["project_snapshot"]["manifest"] for case in current["cases"]]
        for row in report["rows"]:
            project = root / "run/cases" / row["case_id"] / "project"
            assert snapshot_sources(project)["manifest"] == next(case for case in suite["cases"] if case["id"] == row["case_id"])["project_snapshot"]["manifest"]
        return {"status": "passed", "cases": 4, "splits": {"development": 2, "holdout": 2}, "outcomes": outcomes,
            "public_pass_but_two_private_regressions_detected": True, "partial_and_no_edit_not_called_full_repair": True,
            "private_tests_absent_from_all_model_requests": True, "review_then_actual_subprocess_pytest_no_adoption": True,
            "original_and_frozen_projects_unchanged": True, "fixed_model_calls": 16, "new_network_calls": 0}


def reject(action):
    try:
        action()
    except (ValueError, OSError):
        return
    raise AssertionError("changed/unsupported evaluation accepted")


def verify_guards():
    with tempfile.TemporaryDirectory(prefix="repair-eval-guards-") as temporary:
        root = Path(temporary).resolve()
        suite_dir = root / "suite"
        shutil.copytree(SUITE.parent, suite_dir)
        suite_path = suite_dir / "suite.json"
        model = fixed_model()
        limited = run_evaluation(suite_path, NOTE, output_dir=root / "limited", call_model=model, max_turns=4,
            prices=TokenPrices(Decimal(1), Decimal(1)), max_cost=Decimal("0.000012"))
        assert limited["status"] == "partial" and limited["usage"]["calls"] == 4 and len(model.calls) == 4
        assert limited["metrics"]["all"]["diagnosis_attempted"] == 1
        assert limited["metrics"]["all"]["format"]["not_observed"] == 3
        bad = run_evaluation(suite_path, NOTE, output_dir=root / "malformed", call_model=fixed_model(malformed=True), max_turns=4)
        assert bad["metrics"]["all"]["format"] == {"passed": 3, "failed": 1, "not_observed": 0}
        assert bad["metrics"]["all"]["schema"]["not_observed"] == 1
        reviews = reviews_for(bad, root / "reviews.json")
        write_json_atomic(root / "reviews.json", [{**reviews[0], "proposal_sha256": "0" * 64}])
        reject(lambda: verify_evaluation(root / "malformed", root / "reviews.json", output_dir=root / "wrong-review"))
        assert not (root / "wrong-review").exists()
        write_json_atomic(root / "reviews.json", [])
        not_reviewed = verify_evaluation(root / "malformed", root / "reviews.json", output_dir=root / "unreviewed")
        assert not_reviewed["status"] == "awaiting_review" and not list((root / "unreviewed").rglob("candidate-regression"))
        frozen = root / "malformed/cases/dev-02/regression/test_regression.py"
        raw = frozen.read_bytes(); frozen.write_bytes(raw + b"\n# changed\n")
        reject(lambda: load_run(root / "malformed"))
        frozen.write_bytes(raw)
        def cancelled_model(*args, **options):
            raise KeyboardInterrupt()
        cancelled = run_evaluation(suite_path, NOTE, output_dir=root / "cancelled", call_model=cancelled_model, max_turns=4)
        assert cancelled["status"] == "cancelled" and cancelled["usage"]["calls"] == 1
        assert sum(row["outcome"] == "not_run_cancelled" for row in cancelled["rows"]) == 3
        reject(lambda: run_evaluation(suite_path, NOTE, output_dir=root / "malformed", call_model=fixed_model(), max_turns=4))
        data = json.loads(suite_path.read_text(encoding="utf-8"))
        data["cases"][1]["project"] = data["cases"][0]["project"]
        write_json_atomic(suite_path, data)
        reject(lambda: load_suite(suite_path))
        return {"status": "passed", "shared_budget_stops_later_cases_and_unknown_denominators_kept": True,
            "format_failure_separate_from_unobserved_schema": True, "stale_review_and_private_input_changes_rejected": True,
            "no_review_no_candidate_execution": True, "cancel_preserves_partial_and_stops_later_cases": True,
            "duplicate_projects_and_existing_outputs_rejected": True, "new_network_calls": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or ROOT / "codex/acceptance-runs" / f"repair-eval-{uuid4().hex}.json"
    if output.exists():
        parser.error("输出已存在，未覆盖。")
    result = {"workflow": verify_workflow(), "guards": verify_guards(), "status": "passed"}
    write_json_atomic(output, result)
    print(json.dumps({"status": "passed", "output": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
