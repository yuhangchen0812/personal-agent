"""识别评估项目来源，防止常规公开回执绕过其独立回归要求。"""

from .file_runtime import plain_path, write_json_atomic
from .source_snapshot import digest
from .structured_output import strict_json


def evaluation_origin(source_dir):
    source = plain_path(source_dir)
    marker = source.parent / "evaluation-origin.json"
    if source.name != "project" or source.parent.parent.name != "cases":
        return None
    directory = source.parents[2]
    lock_path, report_path = directory / "inputs.lock.json", directory / "report.json"
    # 第 9 步历史案例没有 marker，仍能从其原评估输入锁识别；不改历史记录。
    if not marker.exists() and not lock_path.exists() and not report_path.exists():
        return None
    if not marker.exists() and not lock_path.exists():
        try:
            record = strict_json(report_path.read_text(encoding="utf-8"))
        except ValueError:
            return None
        if not isinstance(record, dict) or record.get("kind") != "ci_repair_evaluation":
            return None
    raw = plain_path(lock_path).read_bytes()
    lock = strict_json(raw.decode("utf-8"))
    if lock.get("version") != 1 or not any(case["id"] == source.parent.name for case in lock["cases"]):
        raise ValueError("评估来源元数据损坏。")
    result = {"run_dir": str(directory), "case_id": source.parent.name, "inputs_sha256": digest(raw)}
    if marker.exists() and strict_json(plain_path(marker).read_text(encoding="utf-8")) != result:
        raise ValueError("评估项目来源标记变化。")
    if report_path.exists():
        report = strict_json(plain_path(report_path).read_text(encoding="utf-8"))
        if (report.get("kind") != "ci_repair_evaluation" or report.get("directory") != str(directory)
                or report.get("inputs_sha256") != result["inputs_sha256"]):
            raise ValueError("评估项目与原收集记录不一致。")
    return result


def mark_evaluation_projects(directory, lock):
    fingerprint = digest((directory / "inputs.lock.json").read_bytes())
    for case in lock["cases"]:
        write_json_atomic(directory / "cases" / case["id"] / "evaluation-origin.json",
            {"run_dir": str(directory), "case_id": case["id"], "inputs_sha256": fingerprint})


def require_independent_receipt(report):
    if evaluation_origin(report["source_dir"]) is not None:
        raise ValueError("评估案例不能凭公开复验回执采纳；请先生成绑定独立回归的 repair-eval receipt。")
