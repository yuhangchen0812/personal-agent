"""从本地目录导入 CI JSONL，冻结输入并批量运行；按输入派生契约核验实际产物。"""

import argparse
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from uuid import uuid4

from . import task_runner as runner
from .ci_contract import META, LOG, OUTPUT, check_ci_report, read_run_metadata
from .config import get_model_settings, model_config_scope
from .file_runtime import plain_path as _plain_path, tracked_model, usage_snapshot, validate_budget, write_json_atomic
from .permissions import PERMISSION_MODES
from .skills import get_project_skill, load_skill
from .skill_task_eval import FILE_TOOLS, run_file_task
from .usage import TokenPrices, UsageStats, parse_token_price


MAX_INPUT_BYTES = 1024 * 1024
MAX_BATCH_RUNS = 20
METHOD_PATH = Path("examples/tasks/ci-triage/SKILL.md")


def _read_input(path):
    path = _plain_path(path)
    if not path.is_file():
        raise ValueError(f"缺少普通输入文件：{path}")
    with path.open("rb") as source:
        raw = source.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError(f"输入文件超过 {MAX_INPUT_BYTES} 字节上限：{path.name}")
    try:
        text = raw.decode("utf-8")  # 保留 BOM 和 CRLF，复制到工作区后原字节可核对。
    except UnicodeError:
        raise ValueError(f"输入必须为 UTF-8 文本：{path.name}") from None
    return text, {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def load_ci_inputs(*, input_dir=None, batch_dir=None):
    """只读显式目录中固定的两个文件；批量只扫描一层，全部预检后才允许模型请求。"""
    if (input_dir is None) == (batch_dir is None):
        raise ValueError("须且只能选择 input_dir 或 batch_dir。")
    root = _plain_path(input_dir if input_dir is not None else batch_dir)
    if not root.is_dir():
        raise ValueError("输入目录不存在。")
    directories = [root] if input_dir is not None else sorted(
        [entry for entry in root.iterdir() if entry.is_dir()], key=lambda entry: entry.name.casefold())
    if not 1 <= len(directories) <= MAX_BATCH_RUNS:
        raise ValueError(f"输入须含 1～{MAX_BATCH_RUNS} 个运行目录。")
    imported = []
    for index, directory in enumerate(directories):
        directory = _plain_path(directory)
        files, sources = {}, {}
        for relative in (META, LOG):
            files[relative], sources[Path(relative).name] = _read_input(directory / Path(relative).name)
        metadata = read_run_metadata(files[META])
        imported.append({"source": {"directory": str(directory), "name": directory.name, "run_id": metadata["run_id"], "files": sources},
            "case": {"id": f"run-{index + 1:02d}",
                "prompt": f"读取 {META} 和 {LOG}，按 ci-triage 方法生成 {OUTPUT} 并实际读回；只依据本次日志，不修改输入。",
                "files": files, "output_files": [OUTPUT], "required_tools": ["read_file", "write_file"]}})
    return root, imported


def _source_checks(imported):
    checks = []
    for item in imported:
        for name, source in item["source"]["files"].items():
            try:
                _, current = _read_input(source["path"])
                unchanged = current == source
            except (ValueError, OSError):
                unchanged = False
            checks.append({"case_id": item["case"]["id"], "file": name, "unchanged": unchanged})
    return checks


def _render(directory, report):
    write_json_atomic(directory / "report.json", report)
    lines = ["# CI 日志批量分诊", "", f"状态：{report['status']}；执行：{report['validation']}。", "",
        "核验方式：根据冻结输入检查 JSON 契约、最终记录统计与引用；没有预先编写的标准答案或质量分数。", "",
        "验收通过只表示正确描述日志，不表示 CI 测试通过、根因成立或修复有效。", "",
        "| 运行 | 源目录 | 执行 | 契约核验 | 日志观察 |", "| --- | --- | --- | --- | --- |"]
    for row in report["rows"]:
        name = row["source_name"].replace("|", "\\|").replace("\n", " ").replace("\r", " ")
        lines.append(f"| {row['case_id']} | {name} | {row['status']} | {row['contract_validated']} | {row['ci_observation']} |")
    for row in report["rows"]:
        lines.extend(["", f"## {row['case_id']}"])
        if row.get("workspace"):
            lines.append(f"[工作区]({row['workspace']})")
        for check in row["checks"]:
            if check["kind"] == "artifact_json" and check["actual"] is not None:
                value = json.dumps(check["actual"], ensure_ascii=False, indent=2)
                fence = "`" * max(3, max((len(part) for part in re.findall(r"`+", value)), default=0) + 1)
                lines.extend([fence + "json", value, fence])
            if not check["passed"]:
                lines.append(f"未通过检查：{check['kind']}")
    lines.extend(["", "## 运行记录", "```json", json.dumps({key: report.get(key) for key in
        ("summary", "usage", "inputs_unchanged", "method_source_unchanged", "junit_source_unchanged")}, ensure_ascii=False, indent=2), "```", "",
        "这是文件导入与运行期契约核验，不是开发/保留评估、模型训练或候选提升证明。"])
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_ci_logs(*, input_dir=None, batch_dir=None, output_dir=None, skill_name=None, call_model=None,
                max_turns=8, permission_mode="acceptEdits", deny=None, prices=None, max_cost=None, model_overrides=None,
                junit_report=None, junit_run_id=None):
    validate_budget(prices, max_cost)
    if (type(max_turns) is not int or max_turns < 1 or permission_mode not in PERMISSION_MODES
            or any(name not in FILE_TOOLS for name in deny or [])):
        raise ValueError("请求上限、权限或 deny 文件工具无效。")
    if sum(value is not None for value in (input_dir, batch_dir, junit_report)) != 1 or junit_run_id is not None and junit_report is None:
        raise ValueError("须且只能选择单次目录、批量目录或 JUnit XML；junit_run_id 仅用于 XML。")
    if output_dir is not None and _plain_path(output_dir).exists():
        raise ValueError("输出目录已存在，未覆盖。")
    conversion = None
    if junit_report is not None:
        from .junit_import import convert_junit_report
        conversion = convert_junit_report(junit_report, run_id=junit_run_id)
        if conversion["status"] != "converted":
            raise ValueError("转换期间 XML 源文件变化，已保留快照，未请求模型。")
        input_dir = conversion["directory"]
    source_root, imported = load_ci_inputs(input_dir=input_dir, batch_dir=batch_dir)
    method = get_project_skill(skill_name) if skill_name is not None else load_skill(runner.ROOT / METHOD_PATH)
    if method.name != "ci-triage" or method.context != "inline":
        raise ValueError("CI 应用需要同名 inline 项目方法。")
    raw_method = method.file_path.read_bytes()
    identity = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8]
    directory = _plain_path(output_dir if output_dir is not None else runner.ROOT / ".personal/ci-runs" / identity)
    if directory.is_relative_to(source_root) or source_root.is_relative_to(directory):
        raise ValueError("输出目录不能与输入目录嵌套。")
    if directory.exists():
        raise ValueError("输出目录已存在，未覆盖。")
    context = nullcontext() if call_model is not None else model_config_scope(model_overrides or {})
    with context:
        settings = {"protocol": "injected", "model": "fixed callback"} if call_model is not None else get_model_settings()
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "method.md").write_bytes(raw_method)
        stats = UsageStats()
        model = tracked_model(stats, settings, call_model)
        report = {"version": 1, "kind": "ci_import", "id": identity, "directory": str(directory), "mode": "single" if input_dir is not None else "batch",
            "input_root": str(source_root), "inputs": [item["source"] for item in imported],
            "validation": "fixed model / actual imported files" if call_model is not None else "real model / actual imported files",
            "artifact_validation": "input-derived runtime contract; no supplied golden answers", "model": {key: settings[key] for key in
                ("protocol", "model", "thinking", "max_tokens") if key in settings},
            "method": {"name": method.name, "path": str(method.file_path.relative_to(runner.ROOT)), "sha256": hashlib.sha256(raw_method).hexdigest()},
            "permission_mode": permission_mode, "deny": deny or [], "max_turns_per_run": max_turns,
            "status": "running", "rows": [], "usage": usage_snapshot(stats, prices, max_cost)}
        if conversion is not None:
            report["junit_import"] = conversion
        _render(directory, report)
        available = FILE_TOOLS if method.allowed_tools is None else FILE_TOOLS & frozenset(method.allowed_tools)
        stopped = False
        for item in imported:
            case = item["case"]
            if stopped:
                row = {"case_id": case["id"], "status": "not_run", "all_passed": False, "checks": [], "trace": [], "calls": []}
            else:
                workspace = directory / "cases" / case["id"]
                row = run_file_task(case, method, workspace, model, available=available, max_turns=max_turns,
                    permission_mode=permission_mode, deny=deny, prices=prices, check_budget=lambda: stats.check_budget(prices, max_cost),
                    restrict_outputs=True, artifact_validator=check_ci_report)
                runner._audit_reads(row, case, workspace)
                row["workspace"] = workspace.relative_to(directory).as_posix()
                stopped = row["status"] in {"error", "cancelled"}
            row.update(score=None, source_name=item["source"]["name"], contract_validated=row["all_passed"], ci_observation="unvalidated")
            if row["contract_validated"]:
                counts = next(check["actual"]["counts"] for check in row["checks"] if check["kind"] == "artifact_json")
                row["ci_observation"] = "no_valid_tests" if counts["total"] == 0 else "failures_reported" if counts["failed"] else "no_failures_in_log"
            report["rows"].append(row)
            report["usage"] = usage_snapshot(stats, prices, max_cost)
            _render(directory, report)
        report["source_checks"] = _source_checks(imported)
        report["inputs_unchanged"] = all(check["unchanged"] for check in report["source_checks"])
        if conversion is not None:
            from .junit_import import source_matches
            report["junit_source_unchanged"] = source_matches(conversion["source"])
        try:
            report["method_source_unchanged"] = method.file_path.read_bytes() == raw_method
        except OSError:
            report["method_source_unchanged"] = False
        report["summary"] = {"selected": len(imported), "completed": sum(row["status"] == "complete" for row in report["rows"]),
            "validated": sum(row["contract_validated"] for row in report["rows"]), "not_run": sum(row["status"] == "not_run" for row in report["rows"])}
        report["status"] = ("cancelled" if any(row["status"] == "cancelled" for row in report["rows"]) else "partial" if stopped else
            "validated" if report["summary"]["validated"] == len(imported) and report["inputs_unchanged"] and report["method_source_unchanged"]
                and report.get("junit_source_unchanged", True) else "failed")
        _render(directory, report)
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--input-dir", type=Path, help="含 run.json/results.jsonl 的单次运行目录，相对调用方目录")
    inputs.add_argument("--batch-dir", type=Path, help="一层子目录各为一次运行，按目录名排序，最多 20 次")
    inputs.add_argument("--junit-report", type=Path, help="JUnit XML；先保存归一化快照，再实际分诊")
    parser.add_argument("--junit-run-id", help="覆盖 XML 归一化的 run_id，默认 XML 文件名")
    parser.add_argument("--output-dir", type=Path, help="不与输入嵌套的新目录；默认 .personal/ci-runs 下唯一目录")
    parser.add_argument("--skill", dest="skill_name")
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--permission-mode", choices=PERMISSION_MODES, default="acceptEdits")
    parser.add_argument("--deny-tool", choices=sorted(FILE_TOOLS), action="append", dest="deny")
    parser.add_argument("--model")
    parser.add_argument("--max-output-tokens", type=int, default=2048)
    parser.add_argument("--input-price", type=parse_token_price)
    parser.add_argument("--output-price", type=parse_token_price)
    parser.add_argument("--max-cost", type=parse_token_price)
    args = parser.parse_args(argv)
    if (args.input_price is None) != (args.output_price is None) or args.max_cost is not None and args.input_price is None:
        parser.error("单价须成对配置，预算需要两种单价。")
    options = vars(args).copy()
    input_price, output_price = options.pop("input_price"), options.pop("output_price")
    model, max_tokens = options.pop("model"), options.pop("max_output_tokens")
    options["prices"] = TokenPrices(input_price, output_price) if input_price is not None else None
    options["model_overrides"] = {"max_tokens": max_tokens, **({"model": model} if model else {})}
    try:
        report = run_ci_logs(**options)
    except (ValueError, OSError) as error:
        print(f"CI 导入/启动失败：{error}", file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps({"status": report["status"], "summary": report["summary"], "usage": report["usage"],
        "report": str(Path(report["directory"]) / "report.md"), "json": str(Path(report["directory"]) / "report.json"),
        "inputs_unchanged": report["inputs_unchanged"], "method_source_unchanged": report["method_source_unchanged"],
        "junit_source_unchanged": report.get("junit_source_unchanged")}, ensure_ascii=False))
    raise SystemExit(0 if report["status"] == "validated" else 130 if report["status"] == "cancelled" else 1)
