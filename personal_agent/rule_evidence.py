"""显式提交的版本化规则：有界加载、函数/源码绑定、只读引用；不认证来源真实性。"""

import ast
import json
import re

from .file_runtime import plain_path
from .source_snapshot import PREFIX, digest, normalized, snapshot_sources
from .structured_output import strict_json

RULES = "playground/input/rules.json"


def review_rules(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise ValueError("使用规则须明确提供不超过 4000 字符的规则来源/版本/适用范围审阅说明。")
    return value.strip()


def _text(value, limit=4000):
    return isinstance(value, str) and 0 < len(value.strip()) <= limit


def load_rules(path, snapshot):
    path = plain_path(path)
    with path.open("rb") as handle:
        raw = handle.read(65537)
    if len(raw) > 65536:
        raise ValueError("规则文件超过 64 KiB。")
    value = strict_json(raw.decode("utf-8-sig"))
    if (not isinstance(value, dict) or set(value) != {"version", "source", "revision", "rules"}
            or type(value["version"]) is not int or value["version"] != 1 or not _text(value["source"], 1000)
            or not _text(value["revision"], 100) or not isinstance(value["rules"], list) or not 1 <= len(value["rules"]) <= 10):
        raise ValueError("规则需 version=1、source、revision 和 1～10 个 rules。")
    ids, targets = set(), {}
    for rule in value["rules"]:
        if (not isinstance(rule, dict) or set(rule) != {"id", "path", "function", "source_sha256", "statement"}
                or not isinstance(rule["id"], str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", rule["id"])
                or rule["id"] in ids or not _text(rule["statement"]) or not isinstance(rule["path"], str)
                or rule["path"] not in snapshot["files"] or not rule["path"].endswith(".py")
                or not isinstance(rule["function"], str) or not rule["function"].isidentifier()
                or rule["function"].startswith("test") or rule["source_sha256"] != snapshot["manifest"][rule["path"]]["sha256"]):
            raise ValueError("规则 ID、字段、目标 Python 函数或源码指纹无效。")
        try:
            nodes = [node for node in ast.walk(ast.parse(normalized(snapshot["files"][rule["path"]])))
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == rule["function"]]
        except (SyntaxError, ValueError, RecursionError):
            raise ValueError("规则目标不是可解析的 Python 函数。") from None
        target = (rule["path"], rule["function"])
        if len(nodes) != 1 or target in targets:
            raise ValueError("规则目标函数须唯一，同一函数仅接受一个整合后的规则条目。")
        ids.add(rule["id"])
        targets[target] = {"id": rule["id"], "start_line": nodes[0].lineno, "end_line": nodes[0].end_lineno}
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    spans = {}
    for rule in value["rules"]:
        block = "\n".join("    " + line for line in json.dumps(rule, ensure_ascii=False, indent=2, sort_keys=True).splitlines())
        start = text[:text.index(block)].count("\n") + 1
        spans[rule["id"]] = (start, start + block.count("\n"))
    return {"raw": raw, "value": value, "text": text, "spans": spans, "targets": targets,
        "source": {"path": str(path), "sha256": digest(raw), "bytes": len(raw), "source": value["source"], "revision": value["revision"]}}


def report_rules(report, snapshot=None):
    if "rules_source" not in report:
        return None
    info = report["rules_source"]
    note = review_rules(info["review_note"])
    rules = load_rules(info["path"], snapshot or snapshot_sources(report["source_dir"]))
    if {**rules["source"], "review_note": note} != info or (plain_path(report["directory"]) / "rules-source.json").read_bytes() != rules["raw"]:
        raise ValueError("规则来源、版本、原字节或审阅说明变化。")
    return rules


def attach_rules(inputs, rules):
    if rules is None:
        return inputs
    metadata = strict_json(inputs["playground/input/run.json"])
    metadata["rules"] = {"path": RULES, "source": rules["value"]["source"], "revision": rules["value"]["revision"],
        "sha256": rules["source"]["sha256"], "ids": sorted(rules["spans"])}
    return {**inputs, "playground/input/run.json": json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", RULES: rules["text"]}


def check_rule_edits(value, texts, rules):
    for edit in value["edits"]:
        original = texts.get(edit["path"], "")
        position = original.find(edit["old"])
        if position < 0:
            return False
        start = original[:position].count("\n") + 1
        end = start + edit["old"].count("\n")
        expected = {target["id"] for (path, _), target in rules["targets"].items()
            if PREFIX + path == edit["path"] and start <= target["end_line"] and end >= target["start_line"]}
        if set(edit["rule_ids"]) != expected:
            return False
        for identity in expected:
            first, last = rules["spans"][identity]
            if not any(cite["path"] == RULES and cite["start_line"] <= first and cite["end_line"] >= last for cite in edit["evidence"]):
                return False
    return True
