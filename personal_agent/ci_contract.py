"""CI JSONL 输入契约与运行期产物核验；不生成模型回复，不依赖固定标准答案。"""

from collections import Counter
import json

from .skill_task_eval import _invalid_json_constant, _path


META = "playground/input/run.json"
LOG = "playground/input/results.jsonl"
OUTPUT = "playground/output/triage.json"
STATES = ("passed", "failed", "skipped")
REASONS = {"invalid_json", "invalid_record", "invalid_status"}


def parse_json(text):
    return json.loads(text.removeprefix("\ufeff"), parse_constant=_invalid_json_constant)


def read_run_metadata(text):
    value = parse_json(text)
    if not isinstance(value, dict) or not isinstance(value.get("run_id"), str) or not value["run_id"].strip():
        raise ValueError("run.json 必须是含非空字符串 run_id 的 JSON 对象。")
    return value


def read_records(text):
    """按原物理行扫描，保留最后有效记录及其证据；无效行不能覆盖有效记录。"""
    latest, invalid = {}, []
    for line, raw in enumerate(text.removeprefix("\ufeff").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw, parse_constant=_invalid_json_constant)
        except ValueError:
            invalid.append({"line": line, "reason": "invalid_json"})
            continue
        if (not isinstance(value, dict) or not isinstance(value.get("test"), str) or not value["test"].strip()
                or not isinstance(value.get("status"), str) or not isinstance(value.get("message", ""), str)):
            invalid.append({"line": line, "reason": "invalid_record"})
        elif value["status"] not in STATES:
            invalid.append({"line": line, "reason": "invalid_status"})
        else:
            latest[value["test"]] = {"status": value["status"], "message": value.get("message", ""), "line": line}
    return latest, invalid


def report_schema(value):
    if not isinstance(value, dict) or set(value) != {"run_id", "counts", "failures", "invalid_lines"} or not isinstance(value["run_id"], str):
        return False
    counts = value["counts"]
    if (not isinstance(counts, dict) or set(counts) != {"total", *STATES}
            or any(type(number) is not int or number < 0 for number in counts.values())):
        return False
    if not isinstance(value["failures"], list) or not isinstance(value["invalid_lines"], list):
        return False
    return all(isinstance(item, dict) and set(item) == {"test", "evidence", "message"}
        and all(isinstance(item[key], str) for key in item) for item in value["failures"]) and all(
        isinstance(item, dict) and set(item) == {"line", "reason"} and type(item["line"]) is int and item["line"] > 0
        and isinstance(item["reason"], str) and item["reason"] in REASONS for item in value["invalid_lines"])


def check_ci_report(case, workspace):
    """输入派生的确定性核验只在本地执行，不作为 Agent 工具或请求材料。"""
    value, error = None, None
    try:
        value = parse_json(_path(workspace, OUTPUT).read_bytes().decode("utf-8"))
    except (ValueError, OSError, UnicodeError) as failure:
        error = type(failure).__name__
    latest, invalid = read_records(case["files"][LOG])
    run_id = read_run_metadata(case["files"][META])["run_id"]
    totals = Counter(record["status"] for record in latest.values())
    schema = error is None and report_schema(value)
    counts = {"total": len(latest), **{state: totals[state] for state in STATES}}
    failures = [{"test": name, "evidence": f"{LOG}:{record['line']}", "message": record["message"]}
        for name, record in sorted(latest.items()) if record["status"] == "failed"]
    return [
        {"kind": "artifact_json", "path": OUTPUT, "actual": value, "passed": error is None, "error": error},
        {"kind": "report_schema", "passed": schema},
        {"kind": "run_id_matches_input", "passed": bool(schema and value["run_id"] == run_id)},
        {"kind": "counts_match_final_records", "passed": bool(schema and value["counts"] == counts)},
        {"kind": "failures_match_final_records", "passed": bool(schema and value["failures"] == failures)},
        {"kind": "invalid_lines_match_input", "passed": bool(schema and value["invalid_lines"] == invalid)},
    ]
