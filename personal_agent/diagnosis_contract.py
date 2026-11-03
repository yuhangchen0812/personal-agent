"""诊断 JSON、实际读到的证据位置和最小 Python 函数体变更核验；不判断根因语义。"""

import ast
import json
from pathlib import Path
import re

from .source_snapshot import PREFIX


RUN = "playground/input/run.json"
FAILURES = "playground/input/failures.jsonl"
FIELDS = {"path", "start_line", "end_line"}


def _string(value, limit=4000):
    return isinstance(value, str) and 0 < len(value.strip()) <= limit


def _citations(value):
    return isinstance(value, list) and 1 <= len(value) <= 10 and all(
        isinstance(item, dict) and set(item) == FIELDS and isinstance(item["path"], str)
        and type(item["start_line"]) is int and type(item["end_line"]) is int
        and 1 <= item["start_line"] <= item["end_line"] < item["start_line"] + 30 for item in value)


def valid_schema(value, decision_version=1, *, rules=None):
    if (not isinstance(value, dict) or set(value) != {"summary", "failures", "edits", "limitations"} or not _string(value["summary"])
            or not isinstance(value["failures"], list) or not 1 <= len(value["failures"]) <= 20
            or not isinstance(value["edits"], list) or len(value["edits"]) > 8
            or not isinstance(value["limitations"], list) or not 1 <= len(value["limitations"]) <= 10
            or not all(_string(item) for item in value["limitations"])):
        return False
    for item in value["failures"]:
        fields = {"test", "status", "hypothesis", "recommendation", "evidence"} | ({"missing_information"} if decision_version == 2 else set())
        if (not isinstance(item, dict) or set(item) != fields
                or not isinstance(item["status"], str) or item["status"] not in {"suspected", "insufficient_evidence"}
                or not all(_string(item[key]) for key in ("test", "hypothesis", "recommendation")) or not _citations(item["evidence"])):
            return False
        if decision_version == 2 and (not isinstance(item["missing_information"], list) or len(item["missing_information"]) > 8
                or not all(_string(text) for text in item["missing_information"])
                or bool(item["missing_information"]) != (item["status"] == "insufficient_evidence")):
            return False
    for item in value["edits"]:
        fields = {"path", "old", "new", "addresses", "evidence"} | ({"rule_ids"} if rules is not None else set())
        if (not isinstance(item, dict) or set(item) != fields
                or not _string(item["path"]) or not _string(item["old"]) or not isinstance(item["new"], str) or len(item["new"]) > 4000
                or item["old"] == item["new"] or not _citations(item["evidence"])
                or not isinstance(item["addresses"], list) or not item["addresses"] or not all(_string(name) for name in item["addresses"])):
            return False
        if rules is not None and (not isinstance(item["rule_ids"], list) or not all(isinstance(name, str) for name in item["rule_ids"])
                or len(set(item["rule_ids"])) != len(item["rule_ids"]) or not set(item["rule_ids"]) <= set(rules["spans"])):
            return False
    return True


def observed_lines(trace, workspace, texts):
    observed = {path: set() for path in texts}
    for item in trace:
        if item["name"] != "read_file" or not item["succeeded"]:
            continue
        try:
            arguments = json.loads(item["arguments"])
            path = (workspace / arguments["file_path"]).resolve().relative_to(workspace).as_posix()
            if path not in texts or "char_offset" in arguments:
                continue
            lines = texts[path].splitlines()
            for match in re.finditer(r"(?m)^\s*(\d+) \| (.*)$", item["result"]):
                number = int(match[1])
                if 1 <= number <= len(lines) and match[2] == lines[number - 1]:
                    observed[path].add(number)
        except (ValueError, KeyError, TypeError):
            continue
    return observed


class _MaskBodies(ast.NodeTransformer):
    def __init__(self, protected=()):
        self.protected = set(protected)

    def visit_FunctionDef(self, node):
        if not node.name.startswith("test") and node.name not in self.protected:
            node.body = [ast.Pass()]
        return node

    visit_AsyncFunctionDef = visit_FunctionDef


def functions_only(before, after, protected=()):
    """仅允许已有非 test 函数体变化；导入、签名、装饰器、测试及配置保持。"""
    try:
        left = _MaskBodies(protected).visit(ast.parse(before))
        right = _MaskBodies(protected).visit(ast.parse(after))
        return ast.dump(left, include_attributes=False) == ast.dump(right, include_attributes=False)
    except (SyntaxError, ValueError, RecursionError):
        return False


def decision_scope(value):
    addressed = sorted({name for edit in value["edits"] for name in edit["addresses"]})
    remaining = [{"test": item["test"], "status": item["status"], "missing_information": item["missing_information"],
        "recommendation": item["recommendation"]} for item in value["failures"] if item["test"] not in addressed]
    return {"status": "no_candidate" if not value["edits"] else "partial_proposal" if remaining else "proposal_for_all_failures",
        "addressed": addressed, "unresolved": remaining, "verification": "not_run"}


def preserves_unknowns(value, texts, candidate):
    """保留证据不足项引用的函数；函数外引用则保留整个文件。只核验声明一致性。"""
    for item in value["failures"]:
        if item["status"] != "insufficient_evidence":
            continue
        citations = [cite for cite in item["evidence"] if cite["path"].startswith(PREFIX)]
        if not citations:
            return False
        for cite in citations:
            path = cite["path"]
            if path not in candidate:
                continue
            try:
                nodes = [node for node in ast.walk(ast.parse(texts[path])) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.lineno <= cite["end_line"] and node.end_lineno >= cite["start_line"]]
            except (SyntaxError, ValueError, RecursionError):
                return False
            if (not nodes and candidate[path] != texts[path]) or not functions_only(texts[path], candidate[path], {node.name for node in nodes}):
                return False
    return True


def check_diagnosis(value, texts, failed, trace, workspace, *, sent_trace=None, protected=(), decision_version=1, rules=None):
    checks = [{"kind": "diagnosis_schema", "passed": valid_schema(value, decision_version, rules=rules)}]
    if not checks[0]["passed"]:
        return checks, {}, []
    names = [item["test"] for item in value["failures"]]
    checks.append({"kind": "failed_cases_covered_once", "passed": len(names) == len(set(names)) and set(names) == set(failed)})
    observed = observed_lines(trace if sent_trace is None else sent_trace, workspace, texts)
    checks.append({"kind": "both_report_inputs_read", "passed": all(observed[path] for path in (RUN, FAILURES))})
    resolved, valid_citations, linked = [], True, True

    def resolve(items):
        nonlocal valid_citations
        for item in items:
            path, start, end = (item[key] for key in ("path", "start_line", "end_line"))
            lines = texts.get(path, "").splitlines()
            valid = path in texts and end <= len(lines) and all(number in observed[path] for number in range(start, end + 1))
            valid_citations = valid_citations and valid
            if valid:
                resolved.append({**item, "quote": "\n".join(lines[start - 1:end])})
    for item in value["failures"]:
        resolve(item["evidence"])
        own_line = failed.get(item["test"], -1)
        linked = linked and any(cite["path"] == FAILURES and cite["start_line"] <= own_line <= cite["end_line"] for cite in item["evidence"])
        if item["status"] == "suspected":
            linked = linked and any(cite["path"].startswith(PREFIX) for cite in item["evidence"])
    candidate, applicable, linked_edits = {}, True, True
    for edit in value["edits"]:
        resolve(edit["evidence"])
        path = edit["path"]
        if path not in texts or not path.startswith(PREFIX) or Path(path).suffix.casefold() != ".py":
            applicable = False
            continue
        original = texts[path]
        current = candidate.get(path, original)
        if original.count(edit["old"]) != 1 or current.count(edit["old"]) != 1:
            applicable = False
            continue
        start = original[:original.index(edit["old"])].count("\n") + 1
        end = start + edit["old"].count("\n")
        linked_edits = linked_edits and all(name in failed for name in edit["addresses"]) and any(
            cite["path"] == path and cite["start_line"] <= start and cite["end_line"] >= end for cite in edit["evidence"])
        candidate[path] = current.replace(edit["old"], edit["new"], 1)
    policy = all(functions_only(texts[path], text, protected) for path, text in candidate.items())
    checks.extend([
        {"kind": "citations_exist_and_were_read", "passed": valid_citations},
        {"kind": "each_hypothesis_links_report_and_source", "passed": linked},
        {"kind": "edits_are_unique_existing_python_fragments", "passed": applicable},
        {"kind": "edits_link_failed_cases_and_changed_lines", "passed": linked_edits},
        {"kind": "function_bodies_only_tests_and_headers_unchanged", "passed": policy},
        {"kind": "no_tool_errors", "passed": all(item["succeeded"] for item in trace)},
    ])
    if decision_version == 2:
        suspected = {item["test"] for item in value["failures"] if item["status"] == "suspected"}
        checks.extend([
            {"kind": "edits_address_supported_failures_only", "passed": all(set(edit["addresses"]) <= suspected for edit in value["edits"])},
            {"kind": "insufficient_evidence_code_preserved", "passed": preserves_unknowns(value, texts, candidate)},
        ])
    if rules is not None:
        from .rule_evidence import RULES, check_rule_edits
        checks.extend([
            {"kind": "rules_file_actually_read", "passed": all(line in observed.get(RULES, set()) for line in range(1, len(rules["text"].splitlines()) + 1))},
            {"kind": "rule_ids_and_citations_match_changed_functions", "passed": check_rule_edits(value, texts, rules)},
        ])
    return checks, candidate, list({json.dumps(item, ensure_ascii=False, sort_keys=True): item for item in resolved}.values())
