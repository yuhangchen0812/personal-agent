"""独立多项目诊断/修复评估；候选须按实际差异审阅，评估不采纳。"""

import argparse
from contextlib import nullcontext
from copy import deepcopy
import json
from pathlib import Path
import sys

from .ci_diagnosis import load_checked_diagnosis, run_ci_diagnosis
from .config import get_model_settings, model_config_scope
from .configured_tests import copy_snapshot, run_project_tests
from .configured_verification import verify_configured_proposal
from .diagnosis_contract import valid_schema
from .file_runtime import plain_path, usage_snapshot, validate_budget, write_json_atomic
from .junit_import import load_junit_report
from .repair_eval_suite import check_case_rules, check_inputs, freeze_suite, load_suite
from .rule_evidence import report_rules, review_rules
from .repair_verification import review_text
from .source_snapshot import digest, snapshot_sources
from .structured_output import strict_json
from .usage import TokenPrices, UsageStats, parse_token_price


def metrics(rows):
    result = {"selected_cases": len(rows), "diagnosis_attempted": sum(row.get("diagnosis_attempted", False) for row in rows),
        "candidates": sum(row.get("candidate", False) for row in rows), "reviewed_candidates": sum(row.get("reviewed", False) for row in rows),
        "declared_insufficient_failures": sum(row.get("declared_insufficient_failures", 0) for row in rows)}
    for field in ("format", "schema", "evidence", "regression_clean"):
        result[field] = {"passed": sum(row.get(field) is True for row in rows), "failed": sum(row.get(field) is False for row in rows),
            "not_observed": sum(row.get(field) is None for row in rows)}
    result["outcomes"] = {}
    for row in rows:
        key = row["outcome"]
        result["outcomes"][key] = result["outcomes"].get(key, 0) + 1
    return result


def publish(directory, report):
    report["metrics"] = {"all": metrics(report["rows"]), **{split: metrics([row for row in report["rows"] if row["split"] == split])
        for split in ("development", "holdout")}}
    write_json_atomic(directory / "report.json", report)
    lines = ["# 诊断与修复评估", "", f"状态：{report['status']}。", "",
        "计数是当前样本上的机械评估；evaluated 表示测量完成，不表示全部修复。未运行/未观察项单列，不自动采纳候选。", "",
        "| 样本 | 分组 | 格式 | 结构 | 证据 | 独立回归无新增失败 | 结果 |", "| --- | --- | --- | --- | --- | --- | --- |"]
    display = lambda value: "通过" if value is True else "未通过" if value is False else "未观察"
    for row in report["rows"]:
        lines.append("| " + " | ".join([row["case_id"], row["split"], *(display(row.get(key)) for key in ("format", "schema", "evidence", "regression_clean")), row["outcome"]]) + " |")
    lines.extend(["", "[完整记录](report.json)", "", "```json", json.dumps(report["metrics"], ensure_ascii=False, indent=2), "```", "",
        "样本和独立测试为自编；holdout 仅表示项目/历史隔离，不是第三方盲测或普适修复质量。模型未获得独立测试；其源码可由评估者审阅。"])
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _dimensions(directory, diagnosis):
    result = {"format": None, "schema": None, "evidence": None, "candidate": False, "diagnosis_status": diagnosis["status"],
        "failure": diagnosis.get("failure"), "usage": diagnosis["usage"], "declared_insufficient_failures": 0}
    response = directory / "model-response.txt"
    if response.is_file():
        try:
            value = strict_json(response.read_text(encoding="utf-8"))
        except ValueError:
            result["format"] = False
        else:
            result.update(format=True, schema=valid_schema(value, diagnosis.get("decision_contract_version", 1), rules=report_rules(diagnosis)))
    if result["schema"] is True and diagnosis["status"] in {"evidence_checked", "rejected"}:
        result["evidence"] = diagnosis["status"] == "evidence_checked"
    if result["evidence"]:
        load_checked_diagnosis(directory / "report.json")
        result["declared_insufficient_failures"] = sum(item["status"] == "insufficient_evidence" for item in diagnosis["diagnosis"]["failures"])
        if diagnosis["diagnosis"]["edits"]:
            result["candidate"] = snapshot_sources(directory / "candidate-source")["manifest"] != diagnosis["source_manifest"]
        result["outcome"] = "awaiting_review" if result["candidate"] else "no_candidate"
    else:
        result["outcome"] = "diagnosis_rejected" if diagnosis["status"] == "rejected" else "diagnosis_incomplete"
    return result


def run_evaluation(suite_path, review_note, *, output_dir, split="all", call_model=None, model_overrides=None,
                   prices=None, max_cost=None, max_turns=8, rules_review_note=None, parents_path=None):
    note = review_text(review_note)
    validate_budget(prices, max_cost)
    if split not in {"development", "holdout", "all"} or type(max_turns) is not int or max_turns < 2:
        raise ValueError("分组或诊断总轮次无效。")
    suite = load_suite(suite_path)
    has_rules = any(case["rules_input"] is not None for case in suite["cases"] if split == "all" or case["split"] == split)
    rule_note = review_rules(rules_review_note) if has_rules else None
    if not has_rules and rules_review_note is not None:
        raise ValueError("所选案例没有规则文件，不应提供规则审阅说明。")
    parents = None
    if parents_path is not None:
        from .diagnosis_followup import load_parent_map
        parents = load_parent_map(parents_path, [case for case in suite["cases"] if split == "all" or case["split"] == split])
    directory = plain_path(output_dir)
    if directory.exists() or directory.is_relative_to(suite["path"].parent) or suite["path"].parent.is_relative_to(directory):
        raise ValueError("评估输出须为与原套件不嵌套的新目录。")
    if parents is not None and any(directory.is_relative_to(Path(ref["path"]).parent) or Path(ref["path"]).parent.is_relative_to(directory) for ref in parents["references"].values()):
        raise ValueError("接续评估输出不能与父诊断目录嵌套。")
    scope = nullcontext() if call_model is not None else model_config_scope({"max_tokens": 4096, **(model_overrides or {})})
    with scope:
        settings = {"protocol": "injected", "model": "fixed callback"} if call_model is not None else get_model_settings()
        frozen = {key: settings[key] for key in ("protocol", "model", "base_url", "max_tokens") if key in settings}
        if "thinking" in settings:
            frozen["thinking"] = settings["thinking"]["type"]
            if "budget_tokens" in settings["thinking"]:
                frozen["thinking_budget"] = settings["thinking"]["budget_tokens"]
        lock = freeze_suite(suite, directory, split, parents)
        stats = UsageStats()
        report = {"version": 1, "kind": "ci_repair_evaluation", "directory": str(directory), "status": "running", "split": split,
            "name": suite["name"], "review_note": note, "model": {key: settings[key] for key in ("protocol", "model")},
            "validation": "fixed model / actual pytest and tools" if call_model is not None else "real model / actual pytest and tools",
            "inputs_sha256": digest((directory / "inputs.lock.json").read_bytes()), "rows": [
                {"case_id": case["id"], "split": case["split"], "outcome": "not_run"} for case in lock["cases"]]}
        if has_rules:
            report["rules_review_note"] = rule_note
        publish(directory, report)
        cancelled = False
        try:
            for row in report["rows"]:
                if cancelled:
                    row["outcome"] = "not_run_cancelled"
                    continue
                try:
                    stats.check_budget(prices, max_cost)
                except RuntimeError:
                    row["outcome"] = "not_run_budget"
                    continue
                base = directory / "cases" / row["case_id"]
                case = next(case for case in lock["cases"] if case["id"] == row["case_id"])
                try:
                    check_inputs(directory, lock)
                    baseline = run_project_tests(base / "project", base / "test-config.json", note, output_dir=base / "public-baseline")
                    row.update(baseline_status=baseline["status"], baseline_sha256=digest((base / "public-baseline/report.json").read_bytes()))
                    if "origin_junit" in case and baseline["status"] in {"tests_failed", "tests_passed"}:
                        provided = load_junit_report(base / "provided.xml")[2]
                        reproduced = load_junit_report(base / "public-baseline/run.xml")[2]
                        identity = lambda parsed: {item["test"]: item["outcome"] for item in parsed["mapping"]}
                        row["provided_junit_matches_baseline"] = identity(provided) == identity(reproduced)
                        if not row["provided_junit_matches_baseline"]:
                            row["outcome"] = "baseline_mismatch"
                            continue
                    if baseline["status"] != "tests_failed":
                        row["outcome"] = "invalid_baseline"
                        if baseline["status"] == "cancelled":
                            cancelled = True
                        continue
                    row["diagnosis_attempted"] = True
                    diagnosis = run_ci_diagnosis(base / "public-baseline/run.xml", base / "project", output_dir=base / "diagnosis",
                        call_model=call_model, model_overrides=frozen if call_model is None else None, output_mode="json_schema",
                        prices=prices, max_cost=max_cost, max_turns=max_turns, shared_usage=stats,
                        **({"rules_file": base / "rules.json", "rules_review_note": rule_note} if "origin_rules" in case else {}),
                        **({"parent_diagnosis": case["origin_parent"]["path"]} if "origin_parent" in case else {}))
                    row["diagnosis_sha256"] = digest((base / "diagnosis/report.json").read_bytes())
                    row.update(_dimensions(base / "diagnosis", diagnosis))
                    if row["candidate"]:
                        row["proposal_sha256"] = digest((base / "diagnosis/proposal.diff").read_bytes())
                    cancelled = diagnosis["status"] == "cancelled"
                except KeyboardInterrupt:
                    row.update(outcome="cancelled", error_type="KeyboardInterrupt")
                    cancelled = True
                except Exception as error:
                    row.update(outcome="case_error", error_type=type(error).__name__)
                finally:
                    report["usage"] = usage_snapshot(stats, prices, max_cost)
                    publish(directory, report)
        except KeyboardInterrupt:
            cancelled = True
            for item in report["rows"]:
                if item["outcome"] == "not_run":
                    item["outcome"] = "not_run_cancelled"
        report["usage"] = usage_snapshot(stats, prices, max_cost)
        try:
            check_inputs(directory, lock)
            report["inputs_unchanged"] = True
        except KeyboardInterrupt:
            cancelled = True
            report["inputs_unchanged"] = False
        except (ValueError, OSError):
            report["inputs_unchanged"] = False
        report["status"] = ("cancelled" if cancelled else "partial" if not report["inputs_unchanged"] or any(
            row["outcome"] in {"not_run_budget", "case_error", "invalid_baseline", "baseline_mismatch"} for row in report["rows"]) else
            "awaiting_review" if any(row.get("candidate") for row in report["rows"]) else "collected")
        publish(directory, report)
        return report


def load_run(run_dir):
    directory = plain_path(run_dir)
    raw = (directory / "report.json").read_bytes()
    report = strict_json(raw.decode("utf-8"))
    lock_raw = (directory / "inputs.lock.json").read_bytes()
    lock = strict_json(lock_raw.decode("utf-8"))
    if (report.get("version") != 1 or report.get("kind") != "ci_repair_evaluation" or report.get("directory") != str(directory)
            or report.get("status") not in {"awaiting_review", "collected", "partial", "cancelled"}
            or digest(lock_raw) != report.get("inputs_sha256")
            or [(row["case_id"], row["split"]) for row in report["rows"]] != [(case["id"], case["split"]) for case in lock["cases"]]):
        raise ValueError("评估记录/输入指纹/样本顺序无效。")
    check_inputs(directory, lock)
    for row in report["rows"]:
        base = directory / "cases" / row["case_id"]
        if row.get("baseline_sha256") and digest((base / "public-baseline/report.json").read_bytes()) != row["baseline_sha256"]:
            raise ValueError("原公开测试记录变化。")
        case = next(case for case in lock["cases"] if case["id"] == row["case_id"])
        if "provided_junit_matches_baseline" in row:
            identity = lambda parsed: {item["test"]: item["outcome"] for item in parsed["mapping"]}
            if "origin_junit" not in case or row["provided_junit_matches_baseline"] != (identity(load_junit_report(base / "provided.xml")[2]) == identity(load_junit_report(base / "public-baseline/run.xml")[2])):
                raise ValueError("JUnit 与重现结果的匹配摘要变化。")
        if row.get("diagnosis_sha256"):
            path = base / "diagnosis/report.json"
            diagnosis = strict_json(path.read_text(encoding="utf-8"))
            if digest(path.read_bytes()) != row["diagnosis_sha256"]:
                raise ValueError("原诊断记录变化。")
            case = next(case for case in lock["cases"] if case["id"] == row["case_id"])
            check_case_rules(base, case, diagnosis, report.get("rules_review_note"))
            derived = _dimensions(path.parent, diagnosis)
            if any(row.get(key) != value for key, value in derived.items()):
                raise ValueError("评估摘要与原诊断不一致。")
            if row["candidate"] and digest((path.parent / "proposal.diff").read_bytes()) != row["proposal_sha256"]:
                raise ValueError("候选差异变化。")
    return directory, report, lock, digest(raw)


def _regression(before, after):
    original = {item["test"]: item["status"] for item in before["records"]}
    current = {item["test"]: item["status"] for item in after["records"]}
    added = sorted(name for name, state in original.items() if state == "passed" and current.get(name) != "passed"
        or state == "skipped" and current.get(name) == "failed")
    return {"same_test_identities": original.keys() == current.keys(), "new_failures": added,
        "fixed_failures": sorted(name for name, state in original.items() if state == "failed" and current.get(name) == "passed"),
        "remaining_failures": sorted(name for name, state in current.items() if state == "failed" or original.get(name) == "failed" and state != "passed")}


def case_result(receipt, before, after):
    comparison = _regression(before, after)
    public_regression = not receipt["checks"]["original_passes_and_skips_preserved"] or not receipt["checks"]["same_test_identities"]
    clean = comparison["same_test_identities"] and not comparison["new_failures"] and not public_regression
    if not clean:
        outcome = "regression_detected"
    elif receipt["status"] == "not_fixed":
        outcome = "test_fix_failed"
    elif receipt["all_failures_fixed"] and not comparison["remaining_failures"]:
        outcome = "full_fix_verified"
    else:
        outcome = "partial_fix_verified"
    return {"regression": comparison, "regression_clean": bool(clean), "outcome": outcome}


def _read_reviews(path):
    with path.open("rb") as source:
        raw = source.read(65537)
    if len(raw) > 65536:
        raise ValueError("审阅文件超过 64 KiB。")
    return raw


def verify_evaluation(run_dir, reviews, *, output_dir):
    source_dir, original, lock, source_hash = load_run(run_dir)
    reviews_path = plain_path(reviews)
    review_raw = _read_reviews(reviews_path)
    entries = strict_json(review_raw.decode("utf-8-sig"))
    if not isinstance(entries, list):
        raise ValueError("审阅文件须为逐例列表。")
    pending = {row["case_id"]: row for row in original["rows"] if row.get("candidate")}
    approved = {}
    for item in entries:
        if (not isinstance(item, dict) or set(item) != {"case_id", "proposal_sha256", "note"}
                or not isinstance(item["case_id"], str) or item["case_id"] not in pending or item["case_id"] in approved
                or item["proposal_sha256"] != pending[item["case_id"]]["proposal_sha256"]):
            raise ValueError("审阅样本、唯一性或实际差异指纹不匹配。")
        approved[item["case_id"]] = review_text(item["note"])
    directory = plain_path(output_dir)
    # 允许 run/evaluation，但不能写入任何已冻结输入/历史证据目录。
    suite_root = Path(lock["suite_source"]).parent
    if (directory.exists() or directory == source_dir or directory.is_relative_to(source_dir / "cases") or source_dir.is_relative_to(directory)
            or directory.is_relative_to(suite_root) or suite_root.is_relative_to(directory)):
        raise ValueError("复验输出须为不覆盖评估输入/历史的新目录。")
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "reviews.json").write_bytes(review_raw)
    result = {"version": 1, "kind": "ci_repair_evaluation_result", "directory": str(directory), "status": "running",
        "source_run": str(source_dir), "source_run_sha256": source_hash, "reviews_source": str(reviews_path),
        "reviews_sha256": digest(review_raw), "rows": deepcopy(original["rows"]), "usage": original["usage"], "new_model_calls": 0}
    publish(directory, result)
    cancelled = False
    try:
        for row in result["rows"]:
            if not row.get("candidate") or row["case_id"] not in approved or cancelled:
                continue
            base = source_dir / "cases" / row["case_id"]
            target = directory / row["case_id"]
            try:
                load_run(source_dir)
                row["reviewed"] = True
                receipt = verify_configured_proposal(base / "diagnosis/report.json", base / "test-config.json", approved[row["case_id"]], output_dir=target / "public")
                row["public_retest"] = {"status": receipt["status"], "checks": receipt.get("checks"), "summary": receipt.get("summary"),
                    "remaining_unaddressed": receipt.get("remaining_unaddressed"), "receipt_sha256": digest((target / "public/receipt.json").read_bytes())}
                if receipt["status"] not in {"verified_with_test_config", "partial_fix_verified_with_test_config", "not_fixed"}:
                    row["outcome"] = "verification_incomplete"
                    cancelled = receipt["status"] == "cancelled"
                    continue
                load_run(source_dir)
                selected_case = next(case for case in lock["cases"] if case["id"] == row["case_id"])
                if selected_case["regression_manifest"] is None:
                    row["independent_status"] = "not_provided"
                    row["outcome"] = ("public_fix_verified_without_independent" if receipt["all_failures_fixed"] else
                        "public_partial_verified_without_independent" if receipt["status"] != "not_fixed" else "public_test_fix_failed")
                    continue
                regression = snapshot_sources(base / "regression")
                settings = strict_json((base / "test-config.json").read_text(encoding="utf-8-sig"))
                private_config = target / "regression-config.json"
                write_json_atomic(private_config, {**settings, "test_paths": ["evaluation_tests"]})
                runs = {}
                for label, source in (("baseline", base / "project"), ("candidate", base / "diagnosis/candidate-source")):
                    project = target / (label + "-project")
                    copy_snapshot(snapshot_sources(source), project)
                    copy_snapshot(regression, project / "evaluation_tests")
                    runs[label] = run_project_tests(project, private_config, approved[row["case_id"]], output_dir=target / (label + "-regression"))
                    if runs[label]["status"] not in {"tests_passed", "tests_failed"}:
                        cancelled = runs[label]["status"] == "cancelled"
                        break
                row["regression_runs"] = {label: {"status": run["status"], "summary": run.get("summary"), "directory": run["directory"]} for label, run in runs.items()}
                if len(runs) != 2 or any(run["status"] not in {"tests_passed", "tests_failed"} for run in runs.values()):
                    row["outcome"] = "verification_incomplete"
                    continue
                old = load_junit_report(target / "baseline-regression/run.xml")[2]
                new = load_junit_report(target / "candidate-regression/run.xml")[2]
                row.update(case_result(receipt, old, new))
            except KeyboardInterrupt:
                row.update(outcome="verification_cancelled", error_type="KeyboardInterrupt")
                cancelled = True
            except Exception as error:
                row.update(outcome="verification_error", error_type=type(error).__name__)
            finally:
                publish(directory, result)
    except KeyboardInterrupt:
        cancelled = True
    try:
        load_run(source_dir)
        if _read_reviews(reviews_path) != review_raw:
            raise ValueError("复验期间审阅记录变化。")
        result["inputs_unchanged"] = True
    except KeyboardInterrupt:
        cancelled = True
        result["inputs_unchanged"] = False
    except (ValueError, OSError, KeyError, TypeError) as error:
        result.update(inputs_unchanged=False, input_error_type=type(error).__name__)
    incomplete = not result["inputs_unchanged"] or any(row["outcome"].startswith(("verification_", "not_run"))
        or row["outcome"] in {"case_error", "invalid_baseline", "baseline_mismatch"} or (row.get("failure") or {}).get("kind") in {"budget", "cancelled"} for row in result["rows"])
    result["status"] = "cancelled" if cancelled else "partial" if incomplete else "awaiting_review" if any(
        row["outcome"] == "awaiting_review" for row in result["rows"]) else "evaluated"
    publish(directory, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    collect = sub.add_parser("diagnose", help="运行已审阅套件的公开基线和独立模型诊断，不执行候选")
    collect.add_argument("--suite", required=True, type=Path)
    collect.add_argument("--review-note", required=True)
    collect.add_argument("--rules-review-note", help="所选套件案例带 rules 文件时必须明确审阅其来源、版本和适用范围")
    collect.add_argument("--parents", type=Path, dest="parents_path", help="显式父诊断映射，须逐一覆盖所选案例")
    collect.add_argument("--output-dir", required=True, type=Path)
    collect.add_argument("--split", choices=("development", "holdout", "all"), default="all")
    collect.add_argument("--max-turns", type=int, default=8)
    collect.add_argument("--model")
    collect.add_argument("--max-output-tokens", type=int, default=4096)
    collect.add_argument("--input-price", type=parse_token_price)
    collect.add_argument("--output-price", type=parse_token_price)
    collect.add_argument("--max-cost", type=parse_token_price)
    verify = sub.add_parser("verify", help="按逐例差异指纹审阅后测试，结果不自动采纳")
    verify.add_argument("--run-dir", required=True, type=Path)
    verify.add_argument("--reviews", required=True, type=Path)
    verify.add_argument("--output-dir", required=True, type=Path)
    receipt = sub.add_parser("receipt", help="重新核验具体案例的公开/独立回归证据，生成 v4 采纳回执")
    receipt.add_argument("--evaluation-report", required=True, type=Path)
    receipt.add_argument("--case", required=True, dest="case_id")
    receipt.add_argument("--review-note", required=True)
    receipt.add_argument("--output-dir", required=True, type=Path)
    options = vars(parser.parse_args(argv)); action = options.pop("action")
    if action == "diagnose":
        first, second = options.pop("input_price"), options.pop("output_price")
        if (first is None) != (second is None) or options["max_cost"] is not None and first is None:
            parser.error("单价须成对配置，预算需要两种单价。")
        options["prices"] = TokenPrices(first, second) if first is not None else None
        model, maximum = options.pop("model"), options.pop("max_output_tokens")
        options["model_overrides"] = {"max_tokens": maximum, **({"model": model} if model else {})}
        options["suite_path"] = options.pop("suite")
    try:
        if action == "receipt":
            from .repair_eval_gate import issue_evaluation_receipt
            result = issue_evaluation_receipt(**options)
        else:
            result = run_evaluation(**options) if action == "diagnose" else verify_evaluation(**options)
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f"评估操作失败：{error}", file=sys.stderr)
        raise SystemExit(2) from None
    if action == "receipt":
        print(json.dumps({"status": result["status"], "case_id": result["case_id"], "receipt": str(Path(result["directory"]) / "receipt.json"), "new_model_calls": 0, "new_test_runs": 0}, ensure_ascii=False))
        raise SystemExit(0)
    print(json.dumps({"status": result["status"], "metrics": result["metrics"], "report": str(Path(result["directory"]) / "report.json")}, ensure_ascii=False))
    okay = result["status"] in {"evaluated", "collected"} or action == "diagnose" and result["status"] == "awaiting_review"
    raise SystemExit(0 if okay else 130 if result["status"] == "cancelled" else 1)
