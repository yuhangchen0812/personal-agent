"""应用反馈、冻结候选、开发/保留成对回放与显式采纳；复用现有 Skill 记录。"""

import argparse
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

from . import skills, task_runner as runner
from .config import get_model_settings, model_config_scope
from .file_runtime import (tracked_model as _model, usage_snapshot as _usage,
    validate_budget as _budget_options, write_json_atomic as _save_json)
from .permissions import PERMISSION_MODES
from .session import validate_agent_messages
from .skill_decision import parse_decision
from .skill_eval import SkillEvaluator
from .skill_evolution import SkillEvolutionStore
from .skill_task_eval import FILE_TOOLS, TASK_PROMPT, _checks, _inventory, _trace, run_file_task
from .usage import TokenPrices, UsageStats, parse_token_price


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def input_digest(case):
    return digest(json.dumps(case["files"], ensure_ascii=False, sort_keys=True).encode("utf-8"))


def _path(path, area):
    path = (runner.ROOT / Path(path)).absolute()
    if path.resolve() != path or not path.is_relative_to(runner.ROOT / ".personal" / area):
        raise ValueError("记录须位于当前项目的原运行目录，不能链接或跳转。")
    return path


def _read(path, area):
    path = _path(path, area)
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("应用记录格式或版本无效。")
    return path, value, digest(raw)


def _write(directory, report):
    directory.mkdir(parents=True, exist_ok=True)
    _save_json(directory / "report.json", report)
    rows = report.get("rows", [])
    text = ["# 应用 Skill 改进记录", "", f"状态：{report['status']}；类型：{report['kind']}。", "",
            f"判定：{report.get('decision', '尚无成对结论')}。", "",
            "| 案例 | 方法 | 执行 | 实际验收 |", "| --- | --- | --- | --- |"]
    text += [f"| {row['case_id']} | {row['variant']} | {row['status']} | {row['all_passed']} |" for row in rows]
    text += ["", "```json", json.dumps({key: value for key, value in report.items() if key in
        {"candidate_id", "summary", "usage", "error", "active_skill_unchanged", "source_unchanged"}}, ensure_ascii=False, indent=2), "```", "",
        "固定响应只验证流程；自编已观察样本不构成外部盲测或泛化提升。评估不自动采纳。"]
    (directory / "report.md").write_text("\n".join(text) + "\n", encoding="utf-8")


def _directory(kind):
    identity = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8]
    directory = _path(runner.ROOT / ".personal/task-learning" / kind / identity, "task-learning")
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def install_task_skill(package):
    """显式安装包内原字节副本；已有不同版本或同名用户定义不会被接管。"""
    pack = runner.load_task_pack(package)
    name, raw = pack["method"].name, pack["method_raw"]
    destination = runner.ROOT / ".personal/skills" / name / "SKILL.md"
    if destination.parent.exists():
        target = skills.get_project_skill(name)
        if target.file_path != destination or destination.read_bytes() != raw:
            raise ValueError("项目 Skill 已存在且与包内版本不同，未覆盖。")
        return {"status": "existing", "path": str(destination), "sha256": digest(raw)}
    destination = skills.check_project_skill_creation(name)
    destination.parent.parent.mkdir(parents=True, exist_ok=True)
    destination.parent.mkdir()
    created_file = False
    try:
        if skills._skill_path(destination) != (destination, "project"):
            raise ValueError("安装目标路径变化。")
        with destination.open("xb") as target:
            created_file = True
            target.write(raw)
    except BaseException:
        if created_file:
            destination.unlink(missing_ok=True)
        try:
            destination.parent.rmdir()
        except OSError:
            pass
        raise
    finally:
        skills.reset_skill_cache()
    return {"status": "installed", "path": str(destination), "sha256": digest(raw)}


def _inspect(row, case, directory):
    """从消息、工具轨迹和磁盘重新核对；不信任 JSON 里单独写出的 passed。"""
    name = row.get("workspace")
    if not isinstance(name, str):
        raise ValueError("案例没有工作区。")
    workspace = (directory / name).absolute()
    if workspace.resolve() != workspace or not workspace.is_relative_to(directory / "cases"):
        raise ValueError("案例工作区不在本次运行原目录中。")
    messages = row["messages"]
    if not isinstance(messages, list) or not messages or messages[0].get("role") != "system":
        raise ValueError("文件任务缺少独立方法提示。")
    history = validate_agent_messages(messages[1:])
    if row["status"] != "complete" or row.get("output") != messages[-1]["content"] or row["trace"] != _trace(messages):
        raise ValueError("必须提供完整任务且回答/工具轨迹一致。")
    if len(messages) < 2 or messages[1] != {"role": "user", "content": case["prompt"]}:
        raise ValueError("案例与来源提问不一致。")
    inventory = _inventory(workspace)
    if inventory != row["files_after"]:
        raise ValueError("运行后文件发生变化，不能复用旧验收。")
    checked = {**row, "checks": _checks(case, workspace, row["trace"], inventory)}
    runner._audit_reads(checked, case, workspace)
    if any(checked[key] != row[key] for key in ("checks", "all_passed", "score")):
        raise ValueError("记录中的验收与实际文件/轨迹不一致。")
    return history


def _pack_identity(pack):
    return {"package": pack["directory"].relative_to(runner.ROOT).as_posix(), "manifest": pack["manifest_sha256"],
        "method": digest(pack["method_raw"]), "suites": {key: value["sha256"] for key, value in pack["suites"].items()}}


def record_task_feedback(run, case_id, text, *, rating=None, origin="explicit user feedback"):
    path, report, report_hash = _read(run, "task-runs")
    if report.get("directory") != str(path.parent) or digest((path.parent / "method.md").read_bytes()) != report["method"]["sha256"]:
        raise ValueError("来源目录或方法快照不匹配。")
    pack = runner.load_task_pack(report["package_path"])
    if report["manifest_sha256"] != pack["manifest_sha256"]:
        raise ValueError("来源任务包已变化。")
    rows = [row for row in report["rows"] if row["case_id"] == case_id]
    if len(rows) != 1 or rows[0]["split"] != "development":
        raise ValueError("反馈只能绑定唯一的开发案例；保留集不能用于生成候选。")
    row = rows[0]
    suite = pack["suites"]["development"]
    if report["suites"]["development"] != suite:
        raise ValueError("来源开发样本与当前冻结数据不匹配。")
    case = next(case for case in suite["document"]["cases"] if case["id"] == case_id)
    messages = _inspect(row, case, path.parent)
    store = SkillEvolutionStore()
    prepared = store.prepare_feedback(pack["method"].name, messages)
    if prepared["base_sha256"] != report["method"]["sha256"]:
        raise ValueError("来源方法与当前项目 Skill 版本不一致。")
    prepared["task_source"] = {"report": path.relative_to(runner.ROOT).as_posix(), "sha256": report_hash,
        "run_id": report["id"], "case_id": case_id, "split": "development", "input_sha256": input_digest(case),
        "package": _pack_identity(pack), "origin": origin}
    prepared["task_evidence"] = {"artifacts": [{"path": check["path"], "actual": check["actual"]}
        for check in row["checks"] if check["kind"] == "file"],
        "checks": [{"kind": check["kind"], "passed": check["passed"]} for check in row["checks"]]}
    return store.save_feedback(prepared, text, rating=rating)


def _feedback(feedback_id):
    store = SkillEvolutionStore()
    feedback = store.feedback(feedback_id)
    source = feedback.get("task_source", {})
    if source.get("split") != "development":
        raise ValueError("需要由应用开发任务绑定的明确反馈。")
    path, report, fingerprint = _read(source["report"], "task-runs")
    if fingerprint != source["sha256"]:
        raise ValueError("来源运行报告发生变化。")
    pack = runner.load_task_pack(source["package"]["package"])
    if _pack_identity(pack) != source["package"]:
        raise ValueError("反馈来源任务包已变化。")
    case = next(case for case in pack["suites"]["development"]["document"]["cases"] if case["id"] == source["case_id"])
    row = next(row for row in report["rows"] if row["case_id"] == case["id"])
    _inspect(row, case, path.parent)
    return store, feedback, pack


def draft_task_candidate(feedback_id, *, call_model=None, model_overrides=None, prices=None, max_cost=None):
    _budget_options(prices, max_cost)
    store, feedback, _ = _feedback(feedback_id)
    request = store.prepare_draft(feedback_id)
    directory, stats = _directory("drafts"), UsageStats()
    report = {"version": 1, "kind": "draft", "directory": str(directory), "feedback_id": feedback_id,
        "validation": "fixed callback" if call_model else "real model", "status": "running", "request": request}
    with nullcontext() if call_model else model_config_scope(model_overrides or {}):
        settings = {"protocol": "injected", "model": "fixed callback"} if call_model else get_model_settings()
        report["model"] = {key: settings[key] for key in ("protocol", "model", "thinking", "max_tokens") if key in settings}
        try:
            stats.check_budget(prices, max_cost)
            response = _model(stats, settings, call_model)(request)
            report["response"] = response
            if not isinstance(response, dict) or response.get("role") != "assistant" or response.get("tool_calls"):
                raise ValueError("草稿只能返回纯文本 JSON，工具请求不会执行。")
            decision = parse_decision(json.loads(response["content"]))
            if decision["action"] == "add" or decision["action"] == "merge" and decision["target_skill"] != feedback["skill_name"]:
                raise ValueError("应用闭环只接受本次同名 Skill 的 merge 或 discard。")
            # 返回后重验来源和现有 Skill，再交给既有候选/去重接口。
            _feedback(feedback_id)
            result = store.save_draft(feedback_id, response, model_name=settings["model"], request=request)
            report.update(status="complete", result=result, candidate_id=result["id"] if result["kind"] == "candidate" else None)
        except KeyboardInterrupt:
            report["status"] = "cancelled"
        except Exception as error:
            report.update(status="partial", error={"type": type(error).__name__})
        report["usage"] = _usage(stats, prices, max_cost)
        _write(directory, report)
    return report


def _freeze(package, candidate_id):
    evaluator = SkillEvaluator()
    candidate, feedback, active, methods = evaluator._versions(candidate_id)
    if candidate["status"] != "pending":
        raise ValueError("应用回放/采纳需要 pending 候选；已采纳版本使用 --skill 运行。")
    _, bound, pack = _feedback(feedback["id"])
    if runner.load_task_pack(package)["directory"] != pack["directory"] or candidate["skill_name"] != pack["method"].name:
        raise ValueError("候选与任务包不匹配。")
    frozen = {"candidate_id": candidate_id, "candidate_sha256": digest(evaluator.evolution._path("candidate", candidate_id).read_bytes()),
        "feedback_sha256": digest(evaluator.evolution._path("feedback", feedback["id"]).read_bytes()),
        "base_sha256": candidate["base_sha256"], "package": _pack_identity(pack), "source": bound["task_source"],
        "methods": {name: {"text": method.prompt_template, "sha256": digest(method.prompt_template.encode("utf-8"))} for name, method in methods.items()}}
    return pack, methods, active, frozen


def _cases(pack, split, frozen):
    source = frozen["source"]
    return [case for case in pack["suites"][split]["document"]["cases"]
        if case["id"] != source["case_id"] and input_digest(case) != source["input_sha256"]]


def _summarize(rows):
    return {variant: {"selected": sum(row["variant"] == variant for row in rows),
        "completed": sum(row["variant"] == variant and row["status"] == "complete" for row in rows),
        "passed": sum(row["variant"] == variant and row["all_passed"] for row in rows)} for variant in ("baseline", "candidate")}


def _decision(rows):
    if not rows or any(row["status"] != "complete" for row in rows):
        return "incomplete"
    summary = _summarize(rows)
    base, candidate = (summary[key]["passed"] for key in ("baseline", "candidate"))
    return "candidate_better_on_tasks" if candidate > base else "baseline_better_on_tasks" if base > candidate else "tie_on_tasks"


def _eligible(report):
    summary = report["summary"]["candidate"]
    return (report["status"] == "complete" and report.get("source_unchanged") and report.get("active_skill_unchanged")
        and report["decision"] in {"tie_on_tasks", "candidate_better_on_tasks"} and summary["passed"] == summary["selected"] > 0)


def _comparison(path, pack, frozen, split):
    path, report, fingerprint = _read(path, "task-learning/comparisons")
    if (report.get("directory") != str(path.parent) or report.get("kind") != "comparison" or report.get("split") != split
            or report.get("frozen") != frozen or report.get("status") != "complete" or not report.get("source_unchanged")):
        raise ValueError("需要同一冻结版本的完整成对报告。")
    cases = _cases(pack, split, frozen)
    expected = [(case, variant) for case in cases for variant in ("baseline", "candidate")]
    if len(report["rows"]) != len(expected):
        raise ValueError("成对报告遗漏或增加案例。")
    for row, (case, variant) in zip(report["rows"], expected):
        if row["case_id"] != case["id"] or row["variant"] != variant:
            raise ValueError("成对报告案例或方法不匹配。")
        _inspect(row, case, path.parent)
        method = replace(skills.get_project_skill(pack["method"].name), prompt_template=frozen["methods"][variant]["text"])
        expected_method = skills.resolve_skill_prompt(method, case["prompt"])
        if row["messages"][0] != {"role": "system", "content": TASK_PROMPT + expected_method}:
            raise ValueError("成对报告实际请求与冻结方法不一致。")
    if report["summary"] != _summarize(report["rows"]) or report["decision"] != _decision(report["rows"]):
        raise ValueError("成对报告汇总与逐案结果不一致。")
    if not _eligible(report):
        raise ValueError("候选存在失败或回退，不能进入保留验收/采纳。")
    return report, fingerprint


def compare_task_candidate(package, candidate_id, *, split="development", development_report=None,
                           call_model=None, model_overrides=None, prices=None, max_cost=None,
                           max_turns=8, permission_mode="acceptEdits", deny=None):
    _budget_options(prices, max_cost)
    if split not in {"development", "holdout"} or permission_mode not in PERMISSION_MODES or type(max_turns) is not int or max_turns < 1 or any(name not in FILE_TOOLS for name in deny or []):
        raise ValueError("回放分组、权限、上限或 deny 无效。")
    pack, methods, active, frozen = _freeze(package, candidate_id)
    development, development_hash = None, None
    if split == "holdout":
        if development_report is None:
            raise ValueError("保留回放需要先提供开发集报告。")
        development, development_hash = _comparison(development_report, pack, frozen, "development")
    cases = _cases(pack, split, frozen)
    if not cases:
        raise ValueError("排除反馈源输入后没有可比较样本。")
    directory, stats = _directory("comparisons"), UsageStats()
    report = {"version": 1, "kind": "comparison", "directory": str(directory), "candidate_id": candidate_id,
        "split": split, "frozen": frozen, "selected_cases": [case["id"] for case in cases],
        "excluded_source_case": frozen["source"]["case_id"], "development_report_sha256": development_hash,
        "validation": "fixed callback" if call_model else "real model", "status": "running", "rows": [],
        "permission_mode": permission_mode, "deny": deny or [], "max_turns_per_case": max_turns}
    for variant in methods:
        (directory / f"{variant}.md").write_text(frozen["methods"][variant]["text"], encoding="utf-8")
    with nullcontext() if call_model else model_config_scope(model_overrides or {}):
        settings = {"protocol": "injected", "model": "fixed callback"} if call_model else get_model_settings()
        report["model"] = {key: settings[key] for key in ("protocol", "model", "thinking", "max_tokens") if key in settings}
        if development is not None and (report["model"] != development["model"] or report["validation"] != development["validation"]
                or any(report[key] != development[key] for key in ("permission_mode", "deny", "max_turns_per_case"))):
            raise ValueError("开发/保留回放须使用相同模型设置和执行权限。")
        model, stopped = _model(stats, settings, call_model), False
        for index, case in enumerate(cases):
            for variant, method in methods.items():
                if stopped:
                    row = {"case_id": case["id"], "variant": variant, "status": "not_run", "all_passed": False, "checks": [], "calls": [], "trace": []}
                else:
                    workspace = directory / "cases" / f"case-{index + 1:02d}" / variant
                    available = FILE_TOOLS if method.allowed_tools is None else FILE_TOOLS & frozenset(method.allowed_tools)
                    row = run_file_task(case, method, workspace, model, variant=variant, available=available,
                        permission_mode=permission_mode, deny=deny, max_turns=max_turns, prices=prices,
                        check_budget=lambda: stats.check_budget(prices, max_cost), restrict_outputs=True)
                    runner._audit_reads(row, case, workspace)
                    row["workspace"] = workspace.relative_to(directory).as_posix()
                    stopped = row["status"] in {"cancelled", "error"}
                report["rows"].append(row)
                report["usage"] = _usage(stats, prices, max_cost)
                _write(directory, report)
        try:
            report["source_unchanged"] = _freeze(package, candidate_id)[3] == frozen
        except (ValueError, OSError, KeyError, TypeError, StopIteration):
            report["source_unchanged"] = False
        try:
            report["active_skill_unchanged"] = digest(active.read_bytes()) == frozen["base_sha256"]
        except OSError:
            report["active_skill_unchanged"] = False
        report["summary"] = _summarize(report["rows"])
        report["status"] = "cancelled" if any(row["status"] == "cancelled" for row in report["rows"]) else "partial" if stopped else "complete"
        report["decision"] = _decision(report["rows"]) if report["source_unchanged"] else "not_comparable"
        _write(directory, report)
    return report


def _check_applied(store, candidate, base_sha256):
    snapshot = store.root / ".personal/skill-history" / candidate["skill_name"] / f"{candidate['id']}.md"
    if snapshot.resolve() != snapshot or digest(snapshot.read_bytes()) != base_sha256:
        raise ValueError("已写入方法的原版快照不匹配。")
    before = snapshot.read_bytes()
    current = store._target(candidate).file_path.read_bytes()
    newline = b"\r\n" if b"\r\n" in before else b"\n"
    if not current.startswith(before + newline * 2):
        raise ValueError("当前方法不再是原版加本次经验，未继续采纳。")
    heading, separator, body = current[len(before) + len(newline) * 2:].partition(newline * 2)
    prefix, suffix = "## 手动演化记录（", "，UTC）"
    heading = heading.decode("utf-8")
    if not separator or not heading.startswith(prefix) or not heading.endswith(suffix):
        raise ValueError("采纳记录标题不匹配。")
    datetime.fromisoformat(heading[len(prefix):-len(suffix)])
    note = f"<!-- personal-agent-candidate:{candidate['id']} --> 来源反馈：{candidate['feedback_id']}。{candidate['lesson']}"
    if candidate["version"] != 2:
        note = " ".join(note.split())
    if body != (note + "\n").encode("utf-8").replace(b"\n", newline):
        raise ValueError("已写入的经验与冻结候选不匹配。")
    return current


def adopt_task_candidate(package, candidate_id, development_report, holdout_report, reason):
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 1000:
        raise ValueError("显式采纳须提供非空理由，最多 1000 字符。")
    store = SkillEvolutionStore()
    candidate = store.candidate(candidate_id)
    receipt_path = _path(runner.ROOT / ".personal/task-learning/adoptions" / f"{candidate_id}.json", "task-learning/adoptions")
    if receipt_path.exists():
        _, receipt, _ = _read(receipt_path, "task-learning/adoptions")
        if receipt["status"] == "adopted":
            if candidate["status"] != "adopted" or receipt["candidate_id"] != candidate_id or digest(_check_applied(store, candidate, receipt["base_sha256"])) != receipt["adopted_file_sha256"]:
                raise ValueError("既有采纳回执与当前版本不一致。")
            return receipt
        if receipt["status"] != "adopting" or receipt["candidate_id"] != candidate_id:
            raise ValueError("既有采纳意向无效。")
        frozen = receipt["frozen"]
        _, _, pack = _feedback(candidate["feedback_id"])
        if _pack_identity(runner.load_task_pack(package)) != frozen["package"]:
            raise ValueError("采纳意向中的任务包已变化。")
        ignored = {"status", "snapshot", "adopted_at"}
        if ({key: value for key, value in candidate.items() if key not in ignored} !=
                {key: value for key, value in receipt["candidate_record"].items() if key not in ignored}
                or candidate["status"] not in {"pending", "adopted"}
                or digest(store._path("feedback", candidate["feedback_id"]).read_bytes()) != frozen["feedback_sha256"]):
            raise ValueError("采纳意向中的候选或反馈已变化。")
        if digest(store._target(candidate).file_path.read_bytes()) != frozen["base_sha256"]:
            _check_applied(store, candidate, frozen["base_sha256"])
    else:
        pack, _, _, frozen = _freeze(package, candidate_id)
        receipt = None
    development, dev_hash = _comparison(development_report, pack, frozen, "development")
    holdout, holdout_hash = _comparison(holdout_report, pack, frozen, "holdout")
    if (holdout["development_report_sha256"] != dev_hash or development["model"] != holdout["model"]
            or development["validation"] != holdout["validation"]):
        raise ValueError("保留报告未引用本次开发报告。")
    if receipt is not None and (receipt["development_report_sha256"] != dev_hash or receipt["holdout_report_sha256"] != holdout_hash):
        raise ValueError("恢复采纳时不能更换已审阅报告。")
    if receipt is None:
        receipt = {"version": 1, "kind": "adoption", "status": "adopting", "candidate_id": candidate_id,
            "reason": reason.strip(), "frozen": frozen, "candidate_record": candidate,
            "development_report_sha256": dev_hash, "holdout_report_sha256": holdout_hash}
        # 在变更活动文件前保存意向；回执失败后可重试，继续核对实际文件且不会重复追加。
        _save_json(receipt_path, receipt)
    candidate = store.adopt(candidate_id)
    applied = _check_applied(store, candidate, frozen["base_sha256"])
    receipt = {"version": 1, "kind": "adoption", "status": "adopted", "candidate_id": candidate_id,
        "reason": receipt["reason"], "validation": development["validation"], "development_report_sha256": dev_hash,
        "holdout_report_sha256": holdout_hash, "evaluated_candidate_method_sha256": frozen["methods"]["candidate"]["sha256"],
        "base_sha256": frozen["base_sha256"], "snapshot": candidate["snapshot"],
        "adopted_file_sha256": digest(applied), "adopted_at": candidate["adopted_at"],
        "boundary": "evaluated method text and installed file have separate hashes; installation adds provenance and timestamp; authored observed samples, no generalization claim"}
    _save_json(receipt_path, receipt)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    init = actions.add_parser("init", help="显式安装任务包的项目 Skill 副本")
    init.add_argument("--package", required=True)
    feedback = actions.add_parser("feedback", help="绑定完整开发案例及明确反馈")
    feedback.add_argument("--run", required=True, help="默认 task-runs 下的 report.json")
    feedback.add_argument("--case", required=True)
    feedback.add_argument("--text", required=True)
    feedback.add_argument("--rating")
    draft = actions.add_parser("draft")
    draft.add_argument("--feedback", required=True)
    compare = actions.add_parser("compare")
    compare.add_argument("--package", required=True)
    compare.add_argument("--candidate", required=True)
    compare.add_argument("--split", choices=("development", "holdout"), default="development")
    compare.add_argument("--development-report")
    compare.add_argument("--max-turns", type=int, default=8)
    compare.add_argument("--permission-mode", choices=PERMISSION_MODES, default="acceptEdits")
    compare.add_argument("--deny-tool", choices=sorted(FILE_TOOLS), action="append", dest="deny")
    adopt = actions.add_parser("adopt")
    for name in ("package", "candidate", "development-report", "holdout-report", "reason"):
        adopt.add_argument("--" + name, required=True)
    show = actions.add_parser("show")
    show.add_argument("--candidate", required=True)
    for action in (draft, compare):
        action.add_argument("--model")
        action.add_argument("--max-output-tokens", type=int, default=2048)
        action.add_argument("--input-price", type=parse_token_price)
        action.add_argument("--output-price", type=parse_token_price)
        action.add_argument("--max-cost", type=parse_token_price)
    args = parser.parse_args(argv)
    options = vars(args).copy()
    action = options.pop("action")
    runtime = {}
    if action in {"draft", "compare"}:
        input_price, output_price, max_cost = (options.pop(key) for key in ("input_price", "output_price", "max_cost"))
        if (input_price is None) != (output_price is None) or max_cost is not None and input_price is None:
            parser.error("单价须成对配置，预算需要两种单价。")
        model, max_tokens = options.pop("model"), options.pop("max_output_tokens")
        runtime = {"prices": TokenPrices(input_price, output_price) if input_price is not None else None,
            "max_cost": max_cost, "model_overrides": {"max_tokens": max_tokens, **({"model": model} if model else {})}}
    try:
        if action == "init":
            result = install_task_skill(**options)
        elif action == "feedback":
            options["case_id"] = options.pop("case")
            result = record_task_feedback(**options)
        elif action == "draft":
            result = draft_task_candidate(options.pop("feedback"), **runtime)
        elif action == "compare":
            options["candidate_id"] = options.pop("candidate")
            result = compare_task_candidate(**options, **runtime)
        elif action == "adopt":
            options["candidate_id"] = options.pop("candidate")
            result = adopt_task_candidate(**options)
        else:
            print(SkillEvolutionStore().describe(options["candidate"]))
            return
    except (ValueError, OSError, RuntimeError, KeyError, TypeError, StopIteration) as error:
        print(f"应用学习操作失败（{type(error).__name__}）：{error}", file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps({key: value for key, value in result.items() if key in
        {"status", "id", "candidate_id", "directory", "decision", "summary", "usage", "path", "sha256", "error"}}, ensure_ascii=False))
    raise SystemExit(130 if result.get("status") == "cancelled" else 1 if result.get("status") == "partial" or
        result.get("kind") == "comparison" and not _eligible(result) else 0)
