"""可恢复 CI 任务：冻结导入、显式诊断/接续、审阅复验和本地报告发布；不自动采纳。"""

import argparse
from html import escape
import json
from pathlib import Path
import sys
from uuid import uuid4

from .ci_repair import _source_lock
from .diagnosis_history import build_history, load_history
from .file_runtime import plain_path, write_json_atomic
from .repair_eval import load_run, run_evaluation, verify_evaluation
from .repair_eval_gate import check_evaluation_case, issue_evaluation_receipt
from .repair_eval_suite import check_inputs, freeze_suite, load_suite
from .repair_verification import load_checked_verification, review_text
from .rule_evidence import review_rules
from .source_snapshot import digest
from .structured_output import strict_json
from .usage import TokenPrices, parse_token_price

ELIGIBLE = {"full_fix_verified", "partial_fix_verified"}


def _json(path):
    return strict_json(plain_path(path).read_text(encoding="utf-8"))


def _ref(path):
    path = plain_path(path)
    return {"path": str(path), "sha256": digest(path.read_bytes())}


def _checked(ref):
    if _ref(ref["path"]) != ref:
        raise ValueError("任务引用的文件版本变化。")
    return Path(ref["path"])


def create_task(suite_path, review_note, *, output_dir, rules_review_note=None, parents_path=None, split="all", max_turns=8, _project_import=None):
    note = review_text(review_note)
    if split not in {"all", "development", "holdout"} or type(max_turns) is not int or not 2 <= max_turns <= 50:
        raise ValueError("任务分组或总轮次无效。")
    suite = load_suite(suite_path)
    selected = [case for case in suite["cases"] if split == "all" or case["split"] == split]
    rule_note = review_rules(rules_review_note) if any(case["rules_input"] is not None for case in selected) else None
    if rule_note is None and rules_review_note is not None:
        raise ValueError("所选案例无规则，不应提供规则审阅说明。")
    parents = None
    if parents_path is not None:
        from .diagnosis_followup import load_parent_map
        parents = load_parent_map(parents_path, selected)
    directory = plain_path(output_dir)
    protected = [suite["path"].parent, *(Path(ref["path"]).parent for ref in (parents or {}).get("references", {}).values())]
    if _project_import is not None:
        if (_project_import != _ref(directory / "project-source/origin.json") or suite["path"] != directory / "project-source/suite.json"
                or any((directory / name).exists() for name in ("task.json", "state.json", "inputs"))):
            raise ValueError("直接项目导入的预备目录无效。")
        protected = protected[1:]
    if (directory.exists() and _project_import is None) or any(directory.is_relative_to(path) or path.is_relative_to(directory) for path in protected):
        raise ValueError("任务须使用与原套件/父诊断不嵌套的新目录。")
    directory.mkdir(parents=True, exist_ok=_project_import is not None)
    imported = directory / "inputs"
    lock = freeze_suite(suite, imported, split, parents)
    cases = []
    for case in lock["cases"]:
        base = "cases/" + case["id"]
        entry = {"id": case["id"], "split": case["split"], "project": base + "/project", "test_config": base + "/test-config.json", "regression_tests": base + "/regression" if case["regression_manifest"] is not None else None}
        if "origin_rules" in case:
            entry["rules"] = base + "/rules.json"
        if "origin_junit" in case:
            entry["junit_report"] = base + "/provided.xml"
        cases.append(entry)
    write_json_atomic(imported / "suite.json", {"version": 1, "name": suite["name"][:91] + "-imported", "cases": cases})
    parent_ref = None
    if parents is not None:
        write_json_atomic(imported / "parent-links.json", {"version": 1, "parents": [{"case_id": case["id"], "diagnosis": case["origin_parent"]["path"]} for case in lock["cases"]]})
        parent_ref = _ref(imported / "parent-links.json")
    spec = {"version": 1, "kind": "ci_repair_workflow_task", "directory": str(directory), "review_note": note, "rules_review_note": rule_note,
        "suite": _ref(imported / "suite.json"), "input_lock": _ref(imported / "inputs.lock.json"), "parents": parent_ref, "max_turns": max_turns,
        "boundary": "frozen local suite, explicit actions; retry budget is per diagnosis attempt; no automatic apply or remote deployment"}
    if _project_import is not None:
        spec["project_import"] = _project_import
    write_json_atomic(directory / "task.json", spec)
    state = {"version": 1, "kind": "ci_repair_workflow_state", "directory": str(directory), "task_sha256": digest((directory / "task.json").read_bytes()),
        "status": "imported", "active": None, "attempts": [], "diagnosis": None, "evaluation": None, "publication": None}
    write_json_atomic(directory / "state.json", state)
    _load(directory)
    return state


def _load(directory):
    directory = plain_path(directory)
    spec, state = _json(directory / "task.json"), _json(directory / "state.json")
    if (spec.get("version") != 1 or spec.get("kind") != "ci_repair_workflow_task" or spec.get("directory") != str(directory)
            or state.get("version") != 1 or state.get("kind") != "ci_repair_workflow_state" or state.get("directory") != str(directory)
            or state.get("task_sha256") != digest((directory / "task.json").read_bytes()) or len(state["attempts"]) > 20):
        raise ValueError("任务配置、位置或状态记录损坏。")
    lock = _json(_checked(spec["input_lock"]))
    if "project_import" in spec:
        from .project_import import check_project_import
        check_project_import(_checked(spec["project_import"]))
    check_inputs(directory / "inputs", lock)
    _checked(spec["suite"])
    if spec["parents"]:
        _checked(spec["parents"])
    for name in ("diagnosis", "evaluation", "publication"):
        if state[name]:
            _checked(state[name])
    for attempt in state["attempts"]:
        if not plain_path(attempt["directory"]).is_relative_to(directory / "attempts"):
            raise ValueError("尝试目录越界。")
        if attempt["status"] == "completed" and digest((Path(attempt["directory"]) / "completion.json").read_bytes()) != attempt["completion_sha256"]:
            raise ValueError("已完成阶段的检查点变化。")
    if state["diagnosis"]:
        _run_matches(directory, state["diagnosis"], lock)
    if state["evaluation"]:
        _evaluation(directory, state, lock, state["evaluation"])
    if state["publication"]:
        _validate(directory, state, lock, "publish", {"record": state["publication"]})
    if state["status"] == "published" and not state["publication"] or state["status"] == "evaluated" and not state["evaluation"]:
        raise ValueError("任务状态缺少对应产物。")
    return directory, spec, state, lock


def _save(directory, state):
    write_json_atomic(directory / "state.json", state)


def _run_matches(directory, ref, expected):
    run, original, lock, _ = load_run(_checked(ref).parent)
    if [(item["id"], item["split"]) for item in lock["cases"]] != [(item["id"], item["split"]) for item in expected["cases"]]:
        raise ValueError("诊断案例集合与导入任务不一致。")
    for actual, frozen in zip(lock["cases"], expected["cases"]):
        if any(actual.get(key) != frozen.get(key) for key in ("project_manifest", "regression_manifest", "config_sha256", "origin_parent")):
            raise ValueError("诊断源码、测试、配置或父引用与导入任务不一致。")
        a, b = actual.get("origin_rules"), frozen.get("origin_rules")
        if (a is None) != (b is None) or a and {key:value for key,value in a.items() if key != "path"} != {key:value for key,value in b.items() if key != "path"}:
            raise ValueError("诊断规则与导入任务不一致。")
        a, b = actual.get("origin_junit"), frozen.get("origin_junit")
        if (a is None) != (b is None) or a and {key:value for key,value in a.items() if key != "path"} != {key:value for key,value in b.items() if key != "path"}:
            raise ValueError("诊断 JUnit 与导入任务不一致。")
    return run, original


def _evaluation(directory, state, lock, ref):
    path = _checked(ref)
    value = _json(path)
    run, source = _run_matches(directory, state["diagnosis"], lock)
    if (value.get("kind") != "ci_repair_evaluation_result" or value.get("version") != 1 or value.get("directory") != str(path.parent)
            or value["source_run"] != str(run) or value["source_run_sha256"] != state["diagnosis"]["sha256"]
            or [(row["case_id"],row["split"]) for row in value["rows"]] != [(row["case_id"],row["split"]) for row in source["rows"]]
            or digest(Path(value["reviews_source"]).read_bytes()) != value["reviews_sha256"]
            or (path.parent / "reviews.json").read_bytes() != Path(value["reviews_source"]).read_bytes()):
        raise ValueError("复验来源、案例或审阅文件绑定不一致。")
    for row in value["rows"]:
        if row["outcome"] in ELIGIBLE:
            check_evaluation_case(path, row["case_id"])
    return value


def _validate(directory, state, lock, phase, result):
    ref = result["record"]
    if phase in {"diagnose", "import-run"}:
        _, value = _run_matches(directory, ref, lock)
        if value["status"] not in {"awaiting_review", "collected", "partial", "cancelled"}:
            raise ValueError("诊断未结束。")
        return "diagnosis", ref, "awaiting_review" if value["status"] in {"awaiting_review", "collected"} else "diagnosis_incomplete"
    if phase == "verify":
        value = _evaluation(directory, state, lock, ref)
        return "evaluation", ref, "evaluated" if value["status"] == "evaluated" else "verification_incomplete"
    if phase != "publish":
        raise ValueError("未知任务阶段。")
    value = _json(_checked(ref))
    if value.get("kind") != "ci_workflow_publication" or value["evaluation"] != state["evaluation"]:
        raise ValueError("发布包与任务复验不一致。")
    evaluation = _evaluation(directory, state, lock, state["evaluation"])
    if [(row["case_id"],row["outcome"]) for row in value["cases"]] != [(row["case_id"],row["outcome"]) for row in evaluation["rows"]]:
        raise ValueError("发布包隐藏或改写了测量结果。")
    for case in value["cases"]:
        measured = next(row for row in evaluation["rows"] if row["case_id"] == case["case_id"])
        if "public_retest" in case and case["public_retest"] != measured.get("public_retest"):
            raise ValueError("发布公开测试摘要与原测量不一致。")
        if "independent_status" in case and case["independent_status"] != measured.get("independent_status", "measured" if measured.get("regression_runs") else "not_observed"):
            raise ValueError("发布独立测试状态与原测量不一致。")
        if case.get("receipt"):
            _, receipt = load_checked_verification(_checked(case["receipt"]))
            if receipt["case_id"] != case["case_id"] or receipt["evaluation_report"] != state["evaluation"]["path"]:
                raise ValueError("发布凭据不属于该案例或本轮测量。")
        if bool(case.get("receipt")) != (case["outcome"] in ELIGIBLE):
            raise ValueError("发布凭据与修复结果不一致。")
        if case.get("history"):
            history = load_history(_checked(case["history"]))
            if history["diagnosis"] != str(Path(state["diagnosis"]["path"]).parent / "cases" / case["case_id"] / "diagnosis/report.json"):
                raise ValueError("发布索引属于其他案例。")
    if value.get("all_cases_full_fix_verified") != (bool(value["cases"]) and all(row["outcome"] == "full_fix_verified" for row in value["cases"])):
        raise ValueError("发布包的全修复汇总错误。")
    for name, sha in value["artifacts"].items():
        if name not in {"index.html"} or digest((Path(ref["path"]).parent / name).read_bytes()) != sha:
            raise ValueError("发布展示文件变化。")
    return "publication", ref, "published"


def _complete(directory, state, lock, completion):
    attempt = state["attempts"][state["active"]]
    if completion.get("phase") != attempt["phase"] or completion.get("directory") != attempt["directory"]:
        raise ValueError("完成记录不属于本次阶段。")
    name, ref, status = _validate(directory, state, lock, attempt["phase"], completion["result"])
    state[name] = ref
    if name == "diagnosis":
        state.update(evaluation=None, publication=None)
    elif name == "evaluation":
        state["publication"] = None
    attempt.update(status="completed", result=completion["result"], completion_sha256=digest((Path(attempt["directory"]) / "completion.json").read_bytes()))
    state.update(status=status, active=None)
    _save(directory, state)


def _resume(directory, state, lock):
    if state["active"] is not None:
        attempt = state["attempts"][state["active"]]
        completion = Path(attempt["directory"]) / "completion.json"
        if completion.is_file():
            _complete(directory, state, lock, _json(completion))
        else:
            attempt["status"] = "interrupted"
            state.update(status="interrupted", active=None)
            _save(directory, state)
    return state


def _step(directory, spec, state, lock, phase, action):
    if len(state["attempts"]) >= 20:
        raise ValueError("单任务最多二十次尝试，请保留当前任务后新建。")
    target = directory / "attempts" / (phase + "-" + uuid4().hex)
    state["attempts"].append({"phase": phase, "directory": str(target), "status": "running"})
    state.update(active=len(state["attempts"]) - 1, status=phase + "_running")
    _save(directory, state)
    try:
        result = action(target)
    except Exception as error:
        state["attempts"][-1].update(status="failed", error_type=type(error).__name__)
        state.update(status=phase + "_failed", active=None)
        _save(directory, state)
        raise
    completion = {"phase": phase, "directory": str(target), "result": result}
    write_json_atomic(target / "completion.json", completion)
    _complete(directory, state, lock, completion)
    return state


def _publication(directory, state, lock, target, note):
    evaluation = _evaluation(directory, state, lock, state["evaluation"])
    if evaluation["status"] != "evaluated":
        raise ValueError("完整测量尚未完成，不能发布报告包。")
    target.mkdir(parents=True, exist_ok=False)
    run = Path(state["diagnosis"]["path"]).parent
    cases = []
    for row in evaluation["rows"]:
        name = row["case_id"]
        case = {"case_id": name, "outcome": row["outcome"], "receipt": None, "history": None,
            "public_retest": row.get("public_retest"), "independent_status": row.get("independent_status", "measured" if row.get("regression_runs") else "not_observed")}
        if row["outcome"] in ELIGIBLE:
            issue_evaluation_receipt(state["evaluation"]["path"], name, note, output_dir=target / name / "receipt")
            case["receipt"] = _ref(target / name / "receipt/receipt.json")
        if row.get("diagnosis_status") == "evidence_checked":
            evidence = target / name / "evidence.json"
            write_json_atomic(evidence, {"version": 1, "comparisons": [], "verifications": [{"diagnosis": str(run / "cases" / name / "diagnosis/report.json"), "receipt": case["receipt"]["path"]}] if case["receipt"] else []})
            build_history(run / "cases" / name / "diagnosis/report.json", evidence=evidence, output_dir=target / name / "history")
            case["history"] = _ref(target / name / "history/report.json")
        cases.append(case)
    html = ["<!doctype html><html lang=zh-CN><meta charset=utf-8><title>CI 任务报告</title><style>body{max-width:950px;margin:40px auto;padding:20px;font:16px/1.8 system-ui}article{border:1px solid #ddd;padding:16px;margin:12px 0}</style>",
        "<h1>CI 任务报告</h1><p>published 表示报告包已生成；原测量结果逐例保留。未自动采纳任何候选。</p>"]
    for case in cases:
        html += ["<article><h2>" + escape(case["case_id"]) + "</h2><p>" + escape(case["outcome"]) + "</p>"]
        html += ["<pre>" + escape(json.dumps({"public_retest": case["public_retest"], "independent_status": case["independent_status"]}, ensure_ascii=False, indent=2)) + "</pre>"]
        if case["history"]:
            html.append('<a href="' + escape((Path(case["history"]["path"]).parent / "index.html").as_uri(), quote=True) + '">展开补据与测试证据</a>')
        html.append("</article>")
    html += ['<a href="' + escape(Path(state["evaluation"]["path"]).as_uri(), quote=True) + '">原完整测量结果</a></html>']
    (target / "index.html").write_text("\n".join(html), encoding="utf-8")
    report = {"version": 1, "kind": "ci_workflow_publication", "evaluation": state["evaluation"], "cases": cases,
        "all_cases_full_fix_verified": bool(cases) and all(row["outcome"] == "full_fix_verified" for row in cases),
        "new_model_calls": 0, "new_test_runs": 0, "artifacts": {"index.html": digest((target / "index.html").read_bytes())}}
    write_json_atomic(target / "report.json", report)
    return {"record": _ref(target / "report.json")}


def task_action(task_dir, action, *, retry=False, run_dir=None, reviews=None, review_note=None, call_model=None, model_overrides=None, prices=None, max_cost=None):
    if action == "show":
        return _load(task_dir)[2]
    with _source_lock(plain_path(task_dir)):
        directory, spec, state, lock = _load(task_dir)
        _resume(directory, state, lock)
        if action == "resume":
            return state
        if action in {"diagnose", "import-run"}:
            previous = any(item["phase"] in {"diagnose", "import-run"} for item in state["attempts"])
            if previous and not retry:
                return state
            def diagnose(target):
                if action == "import-run":
                    ref = _ref(plain_path(run_dir) / "report.json")
                    _run_matches(directory, ref, lock)
                else:
                    run_evaluation(spec["suite"]["path"], spec["review_note"], rules_review_note=spec["rules_review_note"],
                        parents_path=spec["parents"]["path"] if spec["parents"] else None, max_turns=spec["max_turns"], output_dir=target,
                        call_model=call_model, model_overrides=model_overrides, prices=prices, max_cost=max_cost)
                    ref = _ref(target / "report.json")
                return {"record": ref}
            return _step(directory, spec, state, lock, action, diagnose)
        if action == "verify":
            if not state["diagnosis"]:
                raise ValueError("须先诊断或明确导入已有诊断。")
            if any(item["phase"] == "verify" for item in state["attempts"]) and not retry:
                return state
            def verify(target):
                _run_matches(directory, state["diagnosis"], lock)
                verify_evaluation(Path(state["diagnosis"]["path"]).parent, reviews, output_dir=target)
                return {"record": _ref(target / "report.json")}
            return _step(directory, spec, state, lock, action, verify)
        if action == "publish":
            if not state["evaluation"]:
                raise ValueError("须先进行明确审阅后的复验。")
            if any(item["phase"] == "publish" for item in state["attempts"]) and not retry:
                return state
            note = review_text(review_note)
            return _step(directory, spec, state, lock, action, lambda target: _publication(directory, state, lock, target, note))
        raise ValueError("未知任务操作。")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    create = sub.add_parser("create", help="冻结导入已审阅套件，尚不请求模型或执行测试")
    create.add_argument("--suite", required=True, type=Path, dest="suite_path")
    create.add_argument("--output-dir", required=True, type=Path)
    create.add_argument("--review-note", required=True)
    create.add_argument("--rules-review-note")
    create.add_argument("--parents", type=Path, dest="parents_path")
    create.add_argument("--split", choices=("all", "development", "holdout"), default="all")
    create.add_argument("--max-turns", type=int, default=8)
    project = sub.add_parser("create-project", help="直接导入项目目录与可选 JUnit，无需手写套件")
    project.add_argument("--source-dir", required=True, type=Path)
    project.add_argument("--output-dir", required=True, type=Path)
    project.add_argument("--review-note", required=True)
    project.add_argument("--test-config", type=Path)
    project.add_argument("--test-path", action="append", dest="test_paths")
    project.add_argument("--regression-tests", type=Path)
    project.add_argument("--junit-report", type=Path)
    project.add_argument("--rules", type=Path, dest="rules_file")
    project.add_argument("--rules-review-note")
    project.add_argument("--parent-diagnosis", type=Path)
    project.add_argument("--case-id", default="project")
    project.add_argument("--max-turns", type=int, default=8)
    for name in ("show", "resume", "diagnose", "import-run", "verify", "publish"):
        command = sub.add_parser(name)
        command.add_argument("--task", required=True, type=Path, dest="task_dir")
        if name in {"diagnose", "import-run", "verify", "publish"}:
            command.add_argument("--retry", action="store_true", help="明确开始新尝试，保留旧产物；诊断可能重复计费，预算按本次尝试")
        if name == "import-run":
            command.add_argument("--run-dir", required=True, type=Path)
        if name == "verify":
            command.add_argument("--reviews", required=True, type=Path)
        if name == "publish":
            command.add_argument("--review-note", required=True)
        if name == "diagnose":
            command.add_argument("--model")
            for flag in ("input-price", "output-price", "max-cost"):
                command.add_argument("--" + flag, type=parse_token_price)
    options = vars(parser.parse_args(argv)); action = options.pop("action")
    if action == "diagnose":
        first, second = options.pop("input_price"), options.pop("output_price")
        if (first is None) != (second is None) or options["max_cost"] is not None and first is None:
            parser.error("价格须成对提供，预算需要两种单价。")
        options["prices"] = TokenPrices(first, second) if first is not None else None
        model = options.pop("model")
        options["model_overrides"] = {"model": model} if model else None
    try:
        if action == "create-project":
            from .project_import import create_project_task
            state = create_project_task(**options)
        else:
            state = create_task(**options) if action == "create" else task_action(action=action, **options)
    except KeyboardInterrupt:
        print("任务中断；用 resume 恢复完成记录，未完成尝试不会自动重放。", file=sys.stderr)
        raise SystemExit(130) from None
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f"任务操作失败：{error}", file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps(state, ensure_ascii=False))
    raise SystemExit(0 if state["status"] in {"imported", "awaiting_review", "evaluated", "published"} else 1)
