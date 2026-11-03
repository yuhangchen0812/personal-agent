"""文件任务包应用：冻结方法/样本，隔离执行并验收实际产物，不创建或采纳 Skill 候选。"""

import argparse
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
from uuid import uuid4

from .config import get_model_settings, model_config_scope
from .file_runtime import tracked_model, usage_snapshot, validate_budget, write_json_atomic
from .permissions import PERMISSION_MODES
from .skills import get_project_skill, load_skill
from .skill_task_eval import FILE_TOOLS, load_tool_suite, run_file_task
from .usage import TokenPrices, UsageStats, parse_token_price


ROOT = Path(__file__).resolve().parent.parent


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _package_path(directory, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative or PurePosixPath(relative).as_posix() != relative:
        raise ValueError("包内文件须使用普通相对路径。")
    name = PurePosixPath(relative)
    if name.is_absolute() or any(part in {".", ".."} or ":" in part for part in name.parts):
        raise ValueError("包内路径不能跳转。")
    path = directory / relative
    if path.resolve() != path or not path.is_file():
        raise ValueError("任务包文件须存在且不能链接重定向。")
    return path


def load_task_pack(package):
    """复用既有 file_tools 样本校验；同时检查 development/holdout 的精确重复输入。"""
    directory = (ROOT / Path(package)).absolute()
    if directory.resolve() != directory or not directory.is_relative_to(ROOT):
        raise ValueError("任务包须位于当前项目原目录。")
    blocked = {"reference", ".personal", ".git", ".venv", "__pycache__", ".idea"}
    if any(part.casefold() in blocked or part.casefold().startswith(".env") for part in directory.relative_to(ROOT).parts):
        raise ValueError("不能从参考源码、环境或运行目录加载任务包。")
    manifest_path = _package_path(directory, "task.json")
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw.decode("utf-8-sig"))
    if (not isinstance(manifest, dict) or set(manifest) != {"version", "name", "method", "suites"}
            or type(manifest["version"]) is not int or manifest["version"] != 1
            or not isinstance(manifest["name"], str) or not manifest["name"].strip()
            or not isinstance(manifest["suites"], dict) or set(manifest["suites"]) != {"development", "holdout"}):
        raise ValueError("任务包须含 version=1、name、method 及 development/holdout 两个样本集。")
    method_path = _package_path(directory, manifest["method"])
    method = load_skill(method_path)
    if method.context != "inline":
        raise ValueError("文件任务包只使用 inline 方法，不执行 fork、资源脚本或额外工具。")
    suites, ids, input_hashes = {}, set(), {}
    for split, relative in manifest["suites"].items():
        path = _package_path(directory, relative)
        suite, source, fingerprint = load_tool_suite(ROOT, path)
        if suite["split"] != split or suite["skill_name"] != method.name:
            raise ValueError("任务包 split 或方法名称与样本不匹配。")
        for case in suite["cases"]:
            digest = _hash(json.dumps(case["files"], ensure_ascii=False, sort_keys=True).encode("utf-8"))
            if case["id"] in ids or digest in input_hashes:
                raise ValueError("development/holdout 的样本 ID 和精确输入不能重复。")
            ids.add(case["id"])
            input_hashes[digest] = split
        suites[split] = {"document": suite, "path": source, "sha256": fingerprint}
    return {"directory": directory, "manifest": manifest, "manifest_sha256": _hash(raw), "method": method,
        "method_raw": method_path.read_bytes(), "suites": suites}


def _save_report(directory, report):
    write_json_atomic(directory / "report.json", report)
    lines = [f"# 文件任务：{report['package_name']}", "", f"状态：{report['status']}；模式：{report['validation']}。", "",
        "产物验收通过表示任务报告正确，不表示日志中的 CI 测试全部通过。", "",
        "| 样本 | 分组 | 执行 | 产物检查 |", "| --- | --- | --- | --- |"]
    for row in report["rows"]:
        lines.append(f"| {row['case_id']} | {row['split']} | {row['status']} | {'通过' if row['all_passed'] else '未通过/未运行'} |")
    for row in report["rows"]:
        lines.extend(["", f"## {row['case_id']}"])
        if row.get("workspace"):
            lines.append(f"工程：[{row['workspace']}]({row['workspace']})")
        for check in row["checks"]:
            if check["kind"] == "file" and check.get("actual") is not None:
                lines.extend(["", f"产物：{check['path']}", "```json", json.dumps(check["actual"], ensure_ascii=False, indent=2), "```"])
            if not check["passed"]:
                lines.append("失败检查：" + json.dumps(check, ensure_ascii=False))
    lines.extend(["", "## 用量", "```json", json.dumps(report["usage"], ensure_ascii=False, indent=2), "```", "",
        "开发/保留样本均为自编任务；未比较候选、未证明能力提升。保留标签不代表外部盲测。"])
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _audit_reads(row, case, workspace):
    """应用任务要求实际读取每个输入，并在最后一次修改后读回每个产物。"""
    events = []
    for index, item in enumerate(row["trace"]):
        try:
            name = json.loads(item["arguments"]).get("file_path")
            if not isinstance(name, str):
                continue
            path = (workspace / name).resolve().relative_to(workspace).as_posix()
            events.append((index, item["name"], path, item["succeeded"]))
        except (ValueError, TypeError, AttributeError):
            continue
    row["checks"].append({"kind": "all_inputs_read", "passed": all(any(name == "read_file" and path == source and ok
        for _, name, path, ok in events) for source in case["files"])})
    for target in case["output_files"] if "output_files" in case else case["expected_files"]:
        last_write = max((index for index, name, path, ok in events if name in {"write_file", "edit_file"} and path == target and ok), default=-1)
        row["checks"].append({"kind": "output_read_after_last_write", "path": target,
            "passed": last_write >= 0 and any(index > last_write and name == "read_file" and path == target and ok for index, name, path, ok in events)})
    if row["status"] == "complete":
        row["all_passed"] = all(check["passed"] for check in row["checks"])
        row["score"] = round(sum(check["passed"] for check in row["checks"]) / len(row["checks"]), 4)


def run_task_pack(package, *, split="development", case_id=None, output_dir=None, call_model=None,
                  max_turns=8, permission_mode="acceptEdits", deny=None, prices=None, max_cost=None, model_overrides=None,
                  skill_name=None):
    """真实模式默认调用当前模型；测试可注入固定模型，但报告明确标记。"""
    pack = load_task_pack(package)
    if skill_name is not None:
        method = get_project_skill(skill_name)
        if method.name != pack["method"].name or method.context != "inline":
            raise ValueError("应用方法须是同名项目 inline Skill。")
        pack.update(method=method, method_raw=method.file_path.read_bytes())
    if split not in {"development", "holdout", "all"} or type(max_turns) is not int or max_turns < 1:
        raise ValueError("split 或 max_turns 无效。")
    if permission_mode not in PERMISSION_MODES or any(name not in FILE_TOOLS for name in deny or []):
        raise ValueError("任务权限或 deny 文件工具名无效。")
    validate_budget(prices, max_cost)
    cases = [(group, case) for group, suite in pack["suites"].items() if split == "all" or group == split
        for case in suite["document"]["cases"] if case_id is None or case["id"] == case_id]
    if not cases:
        raise ValueError("所选分组中没有该样本。")
    injected = call_model is not None
    context = nullcontext() if injected else model_config_scope(model_overrides or {})
    with context:
        settings = {"protocol": "injected", "model": "fixed callback"} if injected else get_model_settings()
        identity = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8]
        directory = Path(output_dir).absolute() if output_dir is not None else ROOT / ".personal/task-runs" / identity
        if directory.resolve() != directory:
            raise ValueError("运行目录不能通过链接重定向。")
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "method.md").write_bytes(pack["method_raw"])
        stats = UsageStats()
        report = {"version": 1, "id": identity, "package_name": pack["manifest"]["name"], "package_path": pack["directory"].relative_to(ROOT).as_posix(),
            "manifest_sha256": pack["manifest_sha256"], "validation": "fixed model / actual file tools" if injected else "real model / actual file tools",
            "model": {key: settings[key] for key in ("protocol", "model", "thinking", "max_tokens") if key in settings},
            "method": {"name": pack["method"].name, "path": str(pack["method"].file_path.relative_to(ROOT)), "sha256": _hash(pack["method_raw"])},
            "suites": {group: value for group, value in pack["suites"].items() if split == "all" or group == split},
            "directory": str(directory), "permission_mode": permission_mode, "deny": deny or [], "max_turns_per_case": max_turns,
            "status": "running", "rows": [], "usage": {}, "boundary": "self-authored samples; no candidate comparison, automatic adoption or generalization claim"}

        def budget():
            stats.check_budget(prices, max_cost)
        model = tracked_model(stats, settings, call_model)
        available = FILE_TOOLS if pack["method"].allowed_tools is None else FILE_TOOLS & frozenset(pack["method"].allowed_tools)
        stopped = False
        for index, (group, case) in enumerate(cases):
            if stopped:
                row = {"case_id": case["id"], "status": "not_run", "all_passed": False, "checks": [], "calls": [], "trace": []}
            else:
                workspace = directory / "cases" / f"case-{index + 1:02d}"
                row = run_file_task(case, pack["method"], workspace, model, available=available, max_turns=max_turns,
                    permission_mode=permission_mode, deny=deny, prices=prices, check_budget=budget, restrict_outputs=True)
                _audit_reads(row, case, workspace)
                row["workspace"] = workspace.relative_to(directory).as_posix()
                stopped = row["status"] in {"cancelled", "error"}
            row["split"] = group
            report["rows"].append(row)
            report["usage"] = usage_snapshot(stats, prices, max_cost)
            _save_report(directory, report)
        report["method_source_unchanged"] = pack["method"].file_path.read_bytes() == pack["method_raw"]
        report["suite_sources_unchanged"] = all(_hash((ROOT / value["path"]).read_bytes()) == value["sha256"] for value in pack["suites"].values())
        report["summary"] = {group: {"selected": sum(row["split"] == group for row in report["rows"]),
            "completed": sum(row["split"] == group and row["status"] == "complete" for row in report["rows"]),
            "passed": sum(row["split"] == group and row["all_passed"] for row in report["rows"])} for group in ("development", "holdout")}
        report["status"] = ("cancelled" if any(row["status"] == "cancelled" for row in report["rows"]) else "partial" if stopped else
            "passed" if all(row["all_passed"] for row in report["rows"]) and report["method_source_unchanged"] and report["suite_sources_unchanged"] else "failed")
        _save_report(directory, report)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog="日志/XML 分诊：task.cmd ci --help；源码诊断：task.cmd diagnose --help；受控修复：task.cmd repair --help；多样本评估：task.cmd repair-eval --help；XML 转换：task.cmd junit --help；Skill 维护：task.cmd learn --help")
    parser.add_argument("--package", type=Path, required=True, help="项目内任务包目录")
    parser.add_argument("--split", choices=("development", "holdout", "all"), default="development")
    parser.add_argument("--case", dest="case_id", help="仅运行一个明确样本")
    parser.add_argument("--skill", dest="skill_name", help="显式使用已安装的同名项目 Skill；默认仍用包内方法")
    parser.add_argument("--output-dir", type=Path, help="尚不存在的新目录，相对调用方目录")
    parser.add_argument("--permission-mode", choices=PERMISSION_MODES, default="acceptEdits")
    parser.add_argument("--deny-tool", choices=sorted(FILE_TOOLS), action="append", dest="deny")
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--model")
    parser.add_argument("--max-output-tokens", type=int, default=2048)
    parser.add_argument("--input-price", type=parse_token_price)
    parser.add_argument("--output-price", type=parse_token_price)
    parser.add_argument("--max-cost", type=parse_token_price)
    args = parser.parse_args()
    if (args.input_price is None) != (args.output_price is None) or args.max_cost is not None and args.input_price is None:
        parser.error("单价须同时配置；预算需要两种单价。")
    prices = TokenPrices(args.input_price, args.output_price) if args.input_price is not None else None
    overrides = {"max_tokens": args.max_output_tokens, **({"model": args.model} if args.model else {})}
    try:
        report = run_task_pack(args.package, split=args.split, case_id=args.case_id, output_dir=args.output_dir, max_turns=args.max_turns,
            permission_mode=args.permission_mode, deny=args.deny, prices=prices, max_cost=args.max_cost, model_overrides=overrides,
            skill_name=args.skill_name)
    except (ValueError, OSError) as error:
        print(f"任务启动失败：{error}", file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps({"status": report["status"], "summary": report["summary"], "report": str(Path(report["directory"]) / "report.md"),
        "json": str(Path(report["directory"]) / "report.json"), "usage": report["usage"]}, ensure_ascii=False))
    raise SystemExit(0 if report["status"] == "passed" else 130 if report["status"] == "cancelled" else 1)


if __name__ == "__main__":
    main()
