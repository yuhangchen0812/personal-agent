"""离线 CI 实战演示：真实工具/pytest/跨进程采纳回滚，固定模型响应，无网络请求。"""

import argparse
from html import escape
import json
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4

from personal_agent import repair_workflow as workflow
from personal_agent.configured_tests import copy_snapshot, run_project_tests
from personal_agent.diagnosis_contract import FAILURES, RUN
from personal_agent.file_runtime import plain_path, write_json_atomic
from personal_agent.repair_eval_suite import load_suite
from personal_agent.source_snapshot import PREFIX, digest, encode_candidate, normalized, snapshot_sources


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/repair-eval/suite.json"
# 此白名单只适用于两个已知演示案例，不是任意模型候选的自动审阅器。
EDITS = {
    "dev-01": ("tags.py", "return text.lower()", "return text.strip().lower()"),
    "holdout-01": ("pages.py", "return len(items) // page_size", "return len(items) // page_size + 1"),
}
NOTE = "仅执行仓库中已审阅的两个自编演示工程，固定候选须另核对精确差异；不访问外部项目。"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def tool_message(*calls):
    return {"role": "assistant", "tool_calls": [
        {"id": identity, "type": "function", "function": {
            "name": "read_file", "arguments": json.dumps({"file_path": path})}}
        for identity, path in calls]}


def observed_lines(text):
    lines = [(int(match[1]), match[2]) for match in re.finditer(r"(?m)^\s*(\d+) \| (.*)$", text)]
    require(bool(lines), "预期读取结果中没有源码行，停止生成固定响应。")
    return lines


class ScriptedModel:
    """替代远端模型的四轮响应；实际文件内容必须来自 Agent 的工具返回值。"""

    def __init__(self):
        self.calls = 0

    def __call__(self, history, definitions, *, response_schema=None, **_):
        raw = json.dumps(history, ensure_ascii=False)
        require("PRIVATE_REGRESSION_ONLY:" not in raw and "evaluation_tests" not in raw,
                "独立回归内容不应进入演示模型请求。")
        self.calls += 1
        results = {message["tool_call_id"]: message["content"] for message in history if message["role"] == "tool"}
        if not results:
            return tool_message(("run", RUN), ("failures", FAILURES))
        metadata = json.loads("\n".join(text for _, text in observed_lines(results["run"])))
        names = sorted(metadata["source_files"])
        if "source-0" not in results:
            return tool_message(*[("source-" + str(index), PREFIX + name) for index, name in enumerate(names)])
        if response_schema is None:
            return {"role": "assistant", "content": "已读取失败报告、公开测试和函数实现；进入固定诊断输出。"}
        require(not definitions, "结构化终结请求不能携带工具。")
        module = next(name for name in names if "/" not in name)
        _, old, new = next(edit for edit in EDITS.values() if edit[0] == module)
        code = observed_lines(results["source-" + str(names.index(module))])
        number = next(number for number, text in code if text.strip() == old)
        source_cite = {"path": PREFIX + module, "start_line": number, "end_line": number}
        failures, edits = [], []
        for line, text in observed_lines(results["failures"]):
            failed = json.loads(text)
            failures.append({"test": failed["test"], "status": "suspected", "missing_information": [],
                "hypothesis": "固定演示：函数返回表达式不能满足已读取的公开失败断言。",
                "recommendation": "将候选交给公开测试与独立回归，不能由公开通过推断完整修复。",
                "evidence": [{"path": FAILURES, "start_line": line, "end_line": line}, source_cite]})
            edits.append({"path": PREFIX + module, "old": old, "new": new,
                "addresses": [failed["test"]], "evidence": [source_cite]})
        return {"role": "assistant", "content": json.dumps({"summary": "预先编写的固定响应，用于演示流程。",
            "failures": failures, "edits": edits, "limitations": ["不是实时模型推理；候选可能引入回归，须独立复验。"]}, ensure_ascii=False)}


def prepare_suite(directory):
    source = load_suite(FIXTURES)
    cases, originals = [], {}
    for case in source["cases"]:
        if case["id"] not in EDITS:
            continue
        name = case["id"]
        base = directory / name
        copy_snapshot(case["project_snapshot"], base / "project")
        copy_snapshot(case["regression_snapshot"], base / "regression")
        (base / "test-config.json").write_bytes(case["config_raw"])
        cases.append({"id": name, "split": case["split"], "project": name + "/project",
            "regression_tests": name + "/regression", "test_config": name + "/test-config.json"})
        originals[name] = {"project": case["project_snapshot"]["manifest"],
            "regression": case["regression_snapshot"]["manifest"], "config": digest(case["config_raw"])}
    require(set(originals) == set(EDITS), "缺少演示输入。")
    write_json_atomic(directory / "suite.json", {"version": 1, "name": "ci-walkthrough-v1", "cases": cases})
    return originals


def review_fixed_candidates(run_dir, report, output):
    """只审阅精确白名单及整个候选字节，测试文件必须保持原样。"""
    reviews = []
    for row in report["rows"]:
        name = row["case_id"]
        require(row.get("candidate") and row.get("evidence") is True, "候选或读取证据未通过。")
        base = run_dir / "cases" / name
        module, old, new = EDITS[name]
        diagnosis = read_json(base / "diagnosis/report.json")
        actual = [(edit["path"], edit["old"], edit["new"]) for edit in diagnosis["diagnosis"]["edits"]]
        require(actual == [(PREFIX + module, old, new)], "候选超出已声明的演示差异。")
        before = snapshot_sources(base / "project")["files"]
        expected = dict(before)
        text = normalized(before[module])
        require(text.count(old) == 1, "原表达式不是唯一匹配。")
        expected[module] = encode_candidate(text.replace(old, new, 1), before[module])
        require(snapshot_sources(base / "diagnosis/candidate-source")["files"] == expected,
                "候选包含额外文件或额外改动。")
        require(digest((base / "diagnosis/proposal.diff").read_bytes()) == row["proposal_sha256"], "差异指纹不一致。")
        reviews.append({"case_id": name, "proposal_sha256": row["proposal_sha256"],
            "note": "固定演示的精确差异白名单及整个候选字节已核对，仅同意在副本复验；不代表人工审阅任意模型。"})
    write_json_atomic(output, reviews)


def cli(directory, label, arguments, expected=0):
    command = [sys.executable, "-X", "utf8", "-B", str(ROOT / "run_task.py"), *map(str, arguments)]
    process = subprocess.run(command, cwd=directory, capture_output=True, timeout=90)
    logs = directory / "commands"
    logs.mkdir(exist_ok=True)
    (logs / (label + ".stdout.log")).write_bytes(process.stdout)
    (logs / (label + ".stderr.log")).write_bytes(process.stderr)
    write_json_atomic(logs / (label + ".json"), {"command": command, "cwd": str(directory),
        "returncode": process.returncode, "expected_returncode": expected})
    require(process.returncode == expected, f"阶段 {label} 退出码 {process.returncode}，请检查 commands 中的日志。")
    return json.loads(process.stdout) if process.stdout else None


def write_overview(directory, report):
    def link(path, label):
        return '<a href="' + escape(Path(path).relative_to(directory).as_posix(), quote=True) + '">' + escape(label) + '</a>'
    rows = []
    for row in report["cases"]:
        rows.append("<tr><td>" + escape(row["case_id"]) + "</td><td>" + escape(row["outcome"]) + "</td><td><pre>" +
            escape(json.dumps({"public": row["public"], "independent_before": row["independent_before"],
                               "independent_after": row["independent_after"]}, ensure_ascii=False, indent=2)) +
            "</pre></td><td>" + link(row["proposal"], "查看候选差异") + "</td></tr>")
    html = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Personal Agent CI 实战演示</title>
<style>body{max-width:1100px;margin:32px auto;padding:16px;font:16px/1.8 system-ui;color:#203040}table{border-collapse:collapse;width:100%}th,td{border:1px solid #ccd5df;padding:12px;text-align:left}pre{font-size:13px;white-space:pre-wrap}a{color:#185bc0}</style>
<h1>CI 实战演示</h1><p>固定模型响应，真实文件读取、pytest、跨进程采纳和回滚。没有请求远端模型。</p>
<p>标签清洗：修复后通过公开和独立测试，已在演示副本采纳、测试并回滚。分页：公开通过，独立测试新增失败，采纳凭据被拒绝。</p>
<table><tr><th>案例</th><th>实际结论</th><th>测试计数</th><th>差异</th></tr>""" + "".join(rows) + "</table><p>" + link(report["publication_html"], "展开原始任务报告") + " · " + link(directory / "report.json", "完整演示记录") + "</p></html>"
    (directory / "index.html").write_text(html, encoding="utf-8")


def run_walkthrough(output_dir):
    if not __debug__:
        raise ValueError("本演示调用的验收逻辑需要保留断言，请勿使用 -O。")
    directory = plain_path(output_dir)
    protected = [ROOT / name for name in ("examples", "personal_agent", "tests", "scripts", ".git", ".venv")]
    require(not directory.exists() and not ROOT.is_relative_to(directory)
            and not any(directory.is_relative_to(path) for path in protected), "请使用不覆盖源码或旧记录的新输出目录。")
    directory.mkdir(parents=True, exist_ok=False)
    report = {"version": 1, "kind": "ci_walkthrough", "status": "running", "directory": str(directory),
        "new_remote_model_calls": 0, "model_mode": "scripted, no provider token measurements",
        "boundary": "two authored, previously observed cases; actual local pytest/tools; not live model quality or external CI acceptance"}
    write_json_atomic(directory / "report.json", report)
    try:
        print("[1/6] 冻结两个自编案例，创建任务。", flush=True)
        originals = prepare_suite(directory / "input-suite")
        task = directory / "task"
        cli(directory, "create", ["workflow", "create", "--suite", directory / "input-suite/suite.json",
            "--output-dir", task, "--review-note", NOTE, "--max-turns", "4"])
        print("[2/6] 固定模型驱动真实 Agent Loop，读取失败报告与源码。", flush=True)
        model = ScriptedModel()
        state = workflow.task_action(task, "diagnose", call_model=model)
        require(state["status"] == "awaiting_review", "诊断没有生成待审阅候选。")
        run_dir = Path(state["diagnosis"]["path"]).parent
        run = read_json(run_dir / "report.json")
        reviews = directory / "reviews.json"
        review_fixed_candidates(run_dir, run, reviews)
        print("[3/6] 精确差异核对通过；运行公开测试和独立回归。", flush=True)
        state = cli(directory, "verify", ["workflow", "verify", "--task", task, "--reviews", reviews])
        require(state["status"] == "evaluated", "复验未完整结束。")
        evaluation = Path(state["evaluation"]["path"])
        measured = read_json(evaluation)
        outcomes = {row["case_id"]: row["outcome"] for row in measured["rows"]}
        require(outcomes == {"dev-01": "full_fix_verified", "holdout-01": "regression_detected"},
                "实际结果不符合演示预期，请保留记录排查。")
        state = cli(directory, "publish", ["workflow", "publish", "--task", task, "--review-note", NOTE])
        publication = read_json(state["publication"]["path"])
        good = next(case for case in publication["cases"] if case["case_id"] == "dev-01")
        bad = next(case for case in publication["cases"] if case["case_id"] == "holdout-01")
        require(good["receipt"] and bad["receipt"] is None, "采纳凭据与测试结果不一致。")
        print("[4/6] 验证分页回归候选无法获得采纳凭据。", flush=True)
        cli(directory, "reject-regression", ["repair-eval", "receipt", "--evaluation-report", evaluation,
            "--case", "holdout-01", "--review-note", NOTE, "--output-dir", directory / "rejected-receipt"], expected=2)
        require(not (directory / "rejected-receipt").exists(), "被拒绝的候选不应产生凭据目录。")
        print("[5/6] 仅采纳演示副本的标签改动，测试后在新进程回滚。", flush=True)
        project = run_dir / "cases/dev-01/project"
        before = snapshot_sources(project)["files"]
        transaction = directory / "adoption"
        try:
            applied = cli(directory, "apply", ["repair", "apply", "--verification", good["receipt"]["path"],
                "--review-note", "明确仅采纳本演示副本中已精确核对且通过独立回归的标签修复。", "--output-dir", transaction])
            require(applied["status"] == "applied", "候选未完成采纳。")
            require(snapshot_sources(project)["files"] == snapshot_sources(run_dir / "cases/dev-01/diagnosis/candidate-source")["files"],
                    "采纳后的字节与已核验候选不一致。")
            post = run_project_tests(project, run_dir / "cases/dev-01/test-config.json", NOTE, output_dir=directory / "post-apply")
            require(post["status"] == "tests_passed", "采纳后公开测试未通过。")
        finally:
            if (transaction / "state.json").exists():
                cli(directory, "rollback", ["repair", "rollback", "--repair-dir", transaction,
                    "--review-note", "演示结束，恢复本次副本的原始字节。"])
        require(snapshot_sources(project)["files"] == before, "回滚后原始字节不一致。")
        restored = run_project_tests(project, run_dir / "cases/dev-01/test-config.json", NOTE, output_dir=directory / "post-rollback")
        require(restored["status"] == "tests_failed", "回滚后未重现原失败。")
        print("[6/6] 新进程恢复任务状态，核对输入与报告，生成演示总览。", flush=True)
        resumed = cli(directory, "resume", ["workflow", "resume", "--task", task])
        require(resumed["status"] == "published" and resumed["attempts"] == state["attempts"], "恢复不应重放任务阶段。")
        current = load_suite(FIXTURES)
        current_originals = {case["id"]: {"project": case["project_snapshot"]["manifest"],
            "regression": case["regression_snapshot"]["manifest"], "config": digest(case["config_raw"])}
            for case in current["cases"] if case["id"] in EDITS}
        require(current_originals == originals, "原 examples 输入变化。")
        frozen = load_suite(directory / "input-suite/suite.json")
        require(all(case["project_snapshot"]["manifest"] == originals[case["id"]]["project"]
                    and case["regression_snapshot"]["manifest"] == originals[case["id"]]["regression"]
                    and digest(case["config_raw"]) == originals[case["id"]]["config"] for case in frozen["cases"]), "演示输入副本变化。")
        cases = []
        for row in measured["rows"]:
            base = run_dir / "cases" / row["case_id"]
            baseline = read_json(base / "public-baseline/report.json")
            cases.append({"case_id": row["case_id"], "outcome": row["outcome"],
                "public_before": baseline["summary"], "public": row["public_retest"]["summary"],
                "independent_before": row["regression_runs"]["baseline"]["summary"],
                "independent_after": row["regression_runs"]["candidate"]["summary"],
                "new_regression_failures": row["regression"]["new_failures"],
                "proposal": str(base / "diagnosis/proposal.diff"), "diagnosis": str(base / "diagnosis/report.json")})
        report.update(status="passed", scripted_model_calls=model.calls, cases=cases, inputs_unchanged=True,
            task=str(task), evaluation_report=str(evaluation), publication_html=str(Path(state["publication"]["path"]).parent / "index.html"),
            regression_receipt_rejected=True, adoption_status=applied["status"], rollback_status=read_json(transaction / "state.json")["status"],
            post_apply=post["summary"], post_rollback=restored["summary"], rollback_bytes_match=True,
            fresh_process_resume_without_phase_replay=True, original_manifests=originals)
        write_json_atomic(directory / "report.json", report)
        write_overview(directory, report)
        return report
    except (Exception, KeyboardInterrupt) as error:
        report.update(status="cancelled" if isinstance(error, KeyboardInterrupt) else "failed", error_type=type(error).__name__, error=str(error))
        write_json_atomic(directory / "report.json", report)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".personal/ci-walkthrough" / uuid4().hex,
        help="新的演示目录；默认 .personal/ci-walkthrough/随机ID。保留全部记录，不覆盖旧目录。")
    options = parser.parse_args()
    try:
        report = run_walkthrough(options.output_dir)
    except KeyboardInterrupt:
        print("演示中断，请保留输出目录检查报告和事务状态。", file=sys.stderr)
        raise SystemExit(130) from None
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"演示失败：{error}", file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps({key: report[key] for key in ("status", "directory", "scripted_model_calls", "new_remote_model_calls")}, ensure_ascii=False))
    print("演示总览：" + str(Path(report["directory"]) / "index.html"))


if __name__ == "__main__":
    main()
