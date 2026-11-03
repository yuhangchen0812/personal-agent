"""有界补据链索引：规则、未覆盖项和已核验测试分列；不运行模型或测试。"""

import argparse
from html import escape
import json
from pathlib import Path
import sys

from .ci_diagnosis import load_checked_diagnosis
from .diagnosis_followup import _rules, _verification, load_comparison
from .file_runtime import plain_path, write_json_atomic
from .source_snapshot import digest, snapshot_sources
from .structured_output import strict_json


def _source(path):
    path = plain_path(path)
    return {"path": str(path), "sha256": digest(path.read_bytes())}


def _evidence(path):
    if path is None:
        return None, {}, {}
    path = plain_path(path)
    with path.open("rb") as handle:
        raw = handle.read(65537)
    value = strict_json(raw.decode("utf-8-sig")) if len(raw) <= 65536 else None
    if (not isinstance(value, dict) or set(value) != {"version", "verifications", "comparisons"} or type(value["version"]) is not int or value["version"] != 1
            or not isinstance(value["verifications"], list) or len(value["verifications"]) > 8 or not isinstance(value["comparisons"], list) or len(value["comparisons"]) > 7):
        raise ValueError("索引证据清单需 version=1、最多八个 verifications 和七个 comparisons，大小不超过 64 KiB。")
    receipts, comparisons = {}, {}
    def resolve(text):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("证据路径须为非空字符串。")
        return plain_path(path.parent / text)
    def add_receipt(diagnosis, receipt):
        if diagnosis in receipts and receipts[diagnosis] != receipt:
            raise ValueError("同一诊断的测试凭据冲突，请明确选择一个。")
        receipts[diagnosis] = receipt
    for item in value["verifications"]:
        if not isinstance(item, dict) or set(item) != {"diagnosis", "receipt"}:
            raise ValueError("verifications 每项须有 diagnosis 和 receipt。")
        diagnosis, receipt = resolve(item["diagnosis"]), resolve(item["receipt"])
        if diagnosis in receipts:
            raise ValueError("证据清单重复声明同一诊断。")
        add_receipt(diagnosis, receipt)
    for item in value["comparisons"]:
        source = resolve(item)
        comparison = load_comparison(source)
        child = Path(comparison["diagnosis"]["path"])
        if child in comparisons:
            raise ValueError("同一子诊断不能有多个比较边。")
        comparisons[child] = (_source(source), comparison)
        for name, receipt_name in (("parent_diagnosis", "parent_verification"), ("diagnosis", "verification")):
            if comparison[receipt_name] is not None:
                add_receipt(Path(comparison[name]["path"]), Path(comparison[receipt_name]["path"]))
    return {"path": str(path), "sha256": digest(raw)}, receipts, comparisons


def history_data(diagnosis, evidence=None):
    latest = plain_path(diagnosis)
    evidence_source, receipts, comparisons = _evidence(evidence)
    reverse, seen, edges, used_comparisons = [], set(), [], set()
    path = latest
    while True:
        if path in seen or len(reverse) >= 8:
            raise ValueError("索引链存在循环或超过八份诊断。")
        seen.add(path)
        report = load_checked_diagnosis(path)
        reverse.append((path, report, _source(path)))
        linked = report.get("parent_diagnosis")
        comparison = comparisons.get(path)
        if comparison is not None:
            source, data = comparison
            used_comparisons.add(path)
            if linked is not None and linked != data["parent_diagnosis"]:
                raise ValueError("比较边与生成时父引用冲突。")
            parent = data["parent_diagnosis"]
            mode = data["link_mode"]
        elif linked is not None:
            parent, mode, source = linked, "generation_bound", None
        else:
            break
        edges.append({"parent": parent, "child": str(path), "mode": mode, "comparison": source})
        path = plain_path(parent["path"])
    if set(receipts) - seen or set(comparisons) != used_comparisons:
        raise ValueError("证据清单含当前链之外的诊断、测试凭据或比较。")
    rounds, previous_rules, previous_candidate = [], {}, None
    for index, (path, report, source) in enumerate(reversed(reverse), 1):
        rules = _rules(report)
        tests, receipt = _verification(receipts.get(path), path)
        addressed = {name for edit in report["diagnosis"]["edits"] for name in edit["addresses"]}
        failures = [{"test": item["test"], "status": item["status"], "addressed": item["test"] in addressed,
            "missing_information": item.get("missing_information", []), "recommendation": item["recommendation"],
            "candidate_retest": tests["candidate_tests"].get(item["test"], "not_observed")} for item in report["diagnosis"]["failures"]]
        candidate = snapshot_sources(Path(report["directory"]) / "candidate-source")["manifest"] if report["diagnosis"]["edits"] else report["source_manifest"]
        base = report["source_manifest"] if previous_candidate is None else previous_candidate
        rounds.append({"round": index, "diagnosis": source, "rules_source": report.get("rules_source"), "rule_ids": sorted(rules),
            "rule_delta": {"added": sorted(rules.keys() - previous_rules.keys()), "removed": sorted(previous_rules.keys() - rules.keys()),
                "changed": sorted(name for name in rules.keys() & previous_rules.keys() if rules[name] != previous_rules[name])},
            "failures": failures, "uncovered_by_proposal": sum(not item["addressed"] for item in failures),
            "tests": tests, "verification": receipt,
            "candidate_files_changed": sorted(name for name in base.keys() | candidate.keys() if base.get(name) != candidate.get(name)),
            "candidate_comparison_base": "original_source" if previous_candidate is None else "previous_candidate",
            "reported_usage_historical": report["usage"]})
        previous_rules, previous_candidate = rules, candidate
    last = rounds[-1]
    summary = {"rounds": len(rounds), "generation_bound_edges": sum(item["mode"] == "generation_bound" for item in edges),
        "explicit_existing_pair_edges": sum(item["mode"] == "explicit_existing_pair" for item in edges),
        "rounds_with_verified_tests": sum(item["verification"] is not None for item in rounds),
        "latest_uncovered_by_proposal": last["uncovered_by_proposal"],
        "latest_test_state": last["tests"]["state"], "latest_all_failures_fixed": last["tests"].get("all_failures_fixed"),
        "latest_candidate_failed_tests": sum(state in {"failure", "error"} for state in last["tests"]["candidate_tests"].values()) if last["verification"] else None}
    bindings = [item["diagnosis"] for item in rounds] + [item["verification"] for item in rounds if item["verification"]] + [item["comparison"] for item in edges if item["comparison"]]
    if evidence_source:
        bindings.append(evidence_source)
    if any(_source(item["path"]) != item for item in bindings):
        raise ValueError("索引收集期间来源变化。")
    return {"diagnosis": str(latest), "evidence_source": evidence_source, "rounds": rounds, "edges": list(reversed(edges)), "summary": summary,
        "boundary": "checked local linear chain; absent test evidence is unknown; independent sets not scored across rounds; no adoption or model-quality claim"}


def _render(data):
    summary = data["summary"]
    lines = ["# 历次补据索引", "", f"共 {summary['rounds']} 轮；最新候选未覆盖 {summary['latest_uncovered_by_proposal']} 项；测试状态 {summary['latest_test_state']}。", "",
        "候选覆盖与实际测试分别记录；未提供测试凭据不计通过。独立用例集合不作跨轮评分。", "",
        "| 轮次 | 新增规则 | 候选未覆盖项 | 已核验测试 |", "| --- | --- | --- | --- |"]
    html = ["<!doctype html><html lang=zh-CN><meta charset=utf-8><title>历次补据索引</title>",
        "<style>body{max-width:1000px;margin:40px auto;padding:0 20px;font:16px/1.7 system-ui;color:#17212b;background:#f5f7fa}details{padding:18px;margin:14px 0;background:white;border:1px solid #dce2e8;border-radius:8px}summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}a{color:#165bb5}</style>",
        "<h1>历次补据索引</h1><p>判断、候选覆盖与实际测试分别显示。缺凭据不计通过；独立测试不作跨轮评分。</p>",
        "<pre>" + escape(json.dumps(summary, ensure_ascii=False, indent=2)) + "</pre>"]
    for row in data["rounds"]:
        added = ", ".join(row["rule_delta"]["added"]) or "—"
        lines.append(f"| {row['round']} | {added} | {row['uncovered_by_proposal']} | {row['tests']['state']} |")
        lines += ["", f"## 第 {row['round']} 轮", "", f"来源：{row['diagnosis']['path']}", "", "```json", json.dumps(row, ensure_ascii=False, indent=2), "```"]
        html += [f"<details><summary>第 {row['round']} 轮 · 未覆盖 {row['uncovered_by_proposal']} 项 · {escape(row['tests']['state'])}</summary>",
            '<p><a href="' + escape(Path(row["diagnosis"]["path"]).as_uri(), quote=True) + '">原始诊断记录</a></p>',
            "<pre>" + escape(json.dumps(row, ensure_ascii=False, indent=2)) + "</pre></details>"]
    html += ["<h2>关联方式</h2><pre>" + escape(json.dumps(data["edges"], ensure_ascii=False, indent=2)) + "</pre></html>"]
    lines += ["", "[可展开索引](index.html) · [完整来源绑定](report.json)", "", "索引只读既有证据，不生成采纳凭据。"]
    return {"report.md": ("\n".join(lines) + "\n").encode("utf-8"), "index.html": ("\n".join(html) + "\n").encode("utf-8")}


def load_history(path):
    path = plain_path(path)
    raw = path.read_bytes()
    value = strict_json(raw.decode("utf-8"))
    if (value.get("version") != 1 or value.get("kind") != "ci_diagnosis_history" or value.get("status") != "indexed" or value.get("directory") != str(path.parent)
            or value.get("new_model_calls") != 0 or value.get("new_test_runs") != 0):
        raise ValueError("历次补据索引无效。")
    data = history_data(value["diagnosis"], (value.get("evidence_source") or {}).get("path"))
    rendered = _render(data)
    if (any(value.get(key) != item for key, item in data.items()) or value.get("artifacts") != {name: digest(content) for name, content in rendered.items()}
            or any((path.parent / name).read_bytes() != content for name, content in rendered.items()) or path.read_bytes() != raw):
        raise ValueError("索引来源、摘要或展示文件变化。")
    return value


def build_history(diagnosis, *, output_dir, evidence=None):
    data = history_data(diagnosis, evidence)
    directory = plain_path(output_dir)
    protected = [Path(item["diagnosis"]["path"]).parent for item in data["rounds"]]
    protected += [Path(item["verification"]["path"]).parent for item in data["rounds"] if item["verification"]]
    protected += [Path(item["comparison"]["path"]).parent for item in data["edges"] if item["comparison"]]
    for row in data["rounds"]:
        report = strict_json(Path(row["diagnosis"]["path"]).read_text(encoding="utf-8"))
        protected.append(Path(report["source_dir"]))
    if directory.exists() or any(directory.is_relative_to(path) or path.is_relative_to(directory) for path in protected):
        raise ValueError("索引输出须为与来源记录、测试和源码不嵌套的新目录。")
    directory.mkdir(parents=True, exist_ok=False)
    rendered = _render(data)
    for name, content in rendered.items():
        (directory / name).write_bytes(content)
    value = {"version": 1, "kind": "ci_diagnosis_history", "directory": str(directory), "status": "indexed", "new_model_calls": 0, "new_test_runs": 0,
        **data, "artifacts": {name: digest(content) for name, content in rendered.items()}}
    write_json_atomic(directory / "report.json", value)
    load_history(directory / "report.json")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diagnosis", required=True, type=Path, help="待索引的最新诊断")
    parser.add_argument("--evidence", type=Path, help="明确列出的测试凭据和已核验比较清单")
    parser.add_argument("--output-dir", required=True, type=Path)
    try:
        value = build_history(**vars(parser.parse_args(argv)))
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f"补据索引失败：{error}", file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps({"status": "indexed", "summary": value["summary"], "index": str(Path(value["directory"]) / "index.html"), "new_model_calls": 0}, ensure_ascii=False))
