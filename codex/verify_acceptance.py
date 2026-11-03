"""当前环境的一键基础验收；默认固定模型，--live 额外请求当前模型。"""

import argparse
from datetime import datetime, timezone
import hashlib
import importlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import traceback
import unittest
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


STAGES = {
    "core_unit": (),
    "delivery": ("codex.verify_delivery:run_joint",),
    "memory_cache": ("codex.verify_runtime_acceptance:verify_joint",),
    "compaction": ("codex.verify_compaction_routes:verify_routes", "codex.verify_compaction_routes:verify_compatibility"),
    "delegation": ("codex.verify_delegation_delivery:run_suite",),
    "file_tasks": ("codex.verify_ci_tasks:verify_workflow", "codex.verify_ci_tasks:verify_failures"),
    "task_learning": ("codex.verify_task_learning:verify_workflow", "codex.verify_task_learning:verify_guards"),
    "ci_import": ("codex.verify_ci_import:verify_workflow", "codex.verify_ci_import:verify_guards"),
    "junit_ci": ("codex.verify_junit_ci:verify_workflow", "codex.verify_junit_ci:verify_guards"),
    "ci_diagnosis": ("codex.verify_ci_diagnosis:verify_workflow", "codex.verify_ci_diagnosis:verify_guards"),
    "ci_repair": ("codex.verify_ci_repair:verify_workflow", "codex.verify_ci_repair:verify_guards",
        "codex.verify_ci_repair:verify_recovery", "codex.verify_ci_repair:verify_cli"),
    "configured_tests": ("codex.verify_configured_tests:verify_workflow", "codex.verify_configured_tests:verify_guards"),
    "structured_diagnosis": ("codex.verify_structured_diagnosis:verify_workflow", "codex.verify_structured_diagnosis:verify_guards"),
    "repair_eval": ("codex.verify_repair_eval:verify_workflow", "codex.verify_repair_eval:verify_guards"),
    "eval_adoption": ("codex.verify_eval_adoption:verify_workflow", "codex.verify_eval_adoption:verify_guards"),
    "evidence_limits": ("codex.verify_evidence_limits:verify_workflow", "codex.verify_evidence_limits:verify_guards"),
    "rule_evidence": ("codex.verify_rule_evidence:verify_workflow", "codex.verify_rule_evidence:verify_guards"),
    "diagnosis_followup": ("codex.verify_diagnosis_followup:verify_workflow", "codex.verify_diagnosis_followup:verify_guards"),
    "diagnosis_history": ("codex.verify_diagnosis_history:verify_workflow",),
    "repair_workflow": ("codex.verify_repair_workflow:verify_workflow",),
    "project_import": ("codex.verify_project_import:verify_workflow",),
    "skill_learning": ("codex.verify_skill_learning:verify_workflow",),
    "delivery_bundle": ("codex.verify_delivery_bundle:verify_workflow",),
    "qwen_thinking": ("codex.verify_qwen_thinking:verify_workflow",),
    "qwen_messages": ("codex.verify_qwen_messages:verify_workflow",),
    "container_delivery": ("codex.verify_container_delivery:verify_workflow",),
    "release_delivery": ("codex.verify_release_delivery:verify_workflow",),
    "protocol_cli": (),
    "live_smoke": ("codex.verify_delivery:run_live",),
}
DEFAULT_STAGES = tuple(name for name in STAGES if name != "live_smoke")
TEST_PATTERNS = (
    "test_anthropic.py", "test_anthropic_stream.py", "test_anthropic_thinking.py", "test_stream_tools.py",
    "test_cli_config.py", "test_single_run.py", "test_request_retry.py", "test_cancellation.py",
    "test_skill_background.py", "test_repl_commands.py", "test_skill_cache.py",
    "test_skill_task_eval.py",
)
CORE_TEST_PATTERNS = (
    "test_agent.py", "test_tool_registry.py", "test_tools.py", "test_permissions.py",
    "test_permission_execution.py", "test_session.py", "test_agent_config.py",
    "test_plan.py", "test_plan_session.py", "test_acceptance.py",
)
EVIDENCE = (
    ("explore_mcp", "delivery-check-2026-10-29.json", ("live", "passed")),
    ("memory_restore", "runtime-acceptance-check-2026-10-29.json", ("live", "status")),
    ("three_compaction_routes", "compaction-routes-check-2026-10-29.json", ("live", "status")),
    ("plan", "delegation-plan-final-2026-10-29.json", ("live", "cases", "plan", "status")),
    ("general", "delegation-delivery-check-2026-10-29.json", ("live", "cases", "general", "status")),
    ("custom", "delegation-delivery-check-2026-10-29.json", ("live", "cases", "custom", "status")),
    ("fork", "delegation-delivery-check-2026-10-29.json", ("live", "cases", "fork", "status")),
    ("ci_triage", "ci-task-check-2026-10-30.json", ("live", "status")),
    ("task_learning", "task-learning-check-2026-10-30.json", ("live", "status")),
    ("ci_import", "ci-import-check-2026-10-30.json", ("live", "status")),
    ("junit_ci", "junit-ci-check-2026-10-30.json", ("live", "status")),
    ("ci_diagnosis", "ci-diagnosis-recovery-2026-10-30.json", ("status",)),
    ("ci_diagnosis_retest", "ci-diagnosis-retest-2026-10-30.json", ("status",)),
    ("structured_diagnosis", "structured-diagnosis-live-check-2026-10-30.json", ("status",)),
    ("structured_retest", "structured-diagnosis-retest-2026-10-30.json", ("status",)),
)
FAILURE_REPORTS = ("runtime-acceptance-attempt1-2026-10-29.json", "runtime-acceptance-attempt2-2026-10-29.json",
    "delegation-delivery-check-2026-10-29.json", "delegation-plan-recheck-2026-10-29.json", "ci-diagnosis-check-2026-10-30.json",
    "configured-tests-live-diagnosis-2026-10-30.json")


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_state():
    files = [*sorted((ROOT / "personal_agent").glob("*.py")), *sorted((ROOT / "codex").glob("verify_*.py")),
        *sorted((ROOT / "tests").glob("test_*.py")),
        *sorted(path for path in (ROOT / "examples/tasks").rglob("*") if path.is_file() and (path.suffix == ".json" or path.name == "SKILL.md")),
        *sorted(path for path in (ROOT / "examples/ci-logs").rglob("*") if path.is_file() and path.suffix in {".json", ".jsonl"}),
        *sorted(path for path in (ROOT / "examples/pytest-ci-demo").rglob("*") if path.is_file() and path.suffix in {".py", ".ini"}),
        *sorted(path for path in (ROOT / "examples/configured-python").rglob("*") if path.is_file() and path.suffix in {".py", ".json"}),
        *sorted(path for path in (ROOT / "examples/repair-eval").rglob("*") if path.is_file() and path.suffix in {".py", ".json"}),
        *sorted(path for path in (ROOT / "examples/evidence-limits").rglob("*") if path.is_file() and path.suffix in {".py", ".json"}),
        *sorted(path for path in (ROOT / "examples/rule-evidence").rglob("*") if path.is_file() and path.suffix in {".py", ".json"}),
        ROOT / "requirements.txt", ROOT / "requirements-dev.txt", ROOT / "run.cmd", ROOT / "run_agent.py", ROOT / "verify.cmd", ROOT / "task.cmd", ROOT / "run_task.py",
        ROOT / "demo.cmd", ROOT / "run_demo.py", ROOT / "Dockerfile", ROOT / ".dockerignore",
        *sorted((ROOT / ".github/workflows").glob("*.yml")),
        *sorted((ROOT / ".github/workflows").glob("*.yaml"))]
    manifest = {path.relative_to(ROOT).as_posix(): sha256(path) for path in files}
    result = {"files": manifest, "sha256": hashlib.sha256(json.dumps(manifest, sort_keys=True).encode("utf-8")).hexdigest()}
    try:
        result["git_head"] = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
        result["working_tree_dirty"] = bool(subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, check=True).stdout)
    except (OSError, subprocess.CalledProcessError):
        result["git_head"] = None
    return result


def environment_state():
    packages = {}
    for package in ("openai", "anthropic", "python-dotenv", "pytest"):
        try:
            packages[package] = version(package)
        except PackageNotFoundError:
            packages[package] = None
    result = {"python": sys.version.split()[0], "executable": sys.executable, "packages": packages,
        "docker_cli_present": shutil.which("docker") is not None, "podman_cli_present": shutil.which("podman") is not None}
    try:
        from personal_agent.config import get_model_settings
        settings = get_model_settings()  # 沿用 .env 加载，只记录非密钥选择和存在性。
        result["model"] = {key: settings[key] for key in ("protocol", "model")}
    except Exception as error:
        result["configuration_error_type"] = type(error).__name__
    result["configuration_presence"] = {key: bool(os.getenv(key, "").strip()) for key in
        ("DASHSCOPE_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL")}
    return result


def historical_evidence():
    rows = []
    for name, filename, keys in EVIDENCE:
        path = ROOT / "codex" / filename
        row = {"name": name, "path": path.relative_to(ROOT).as_posix(), "selector": list(keys), "scope": "historical_not_rerun"}
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            observed = document
            for key in keys:
                observed = observed[key]
            row.update(sha256=sha256(path), observed=observed, report_overall_status=document.get("status"),
                status="passed" if observed is True or observed in ("passed", "validated") else "failed")
        except (OSError, ValueError, KeyError, TypeError) as error:
            row.update(status="failed", error_type=type(error).__name__)
        rows.append(row)
    failures = []
    for filename in FAILURE_REPORTS:
        path = ROOT / "codex" / filename
        failures.append({"path": path.relative_to(ROOT).as_posix(), "exists": path.is_file(),
            "sha256": sha256(path) if path.is_file() else None, "meaning": "preserved failure; some files also contain passed cases"})
    return {"status": "passed" if all(row["status"] == "passed" for row in rows) and all(row["exists"] for row in failures) else "failed",
        "cases": rows, "preserved_failures": failures,
        "boundary": "Only selected JSON fields and file fingerprints are checked; old network calls are not rerun or regraded."}


def worker(stage, result_path):
    """子进程调用已有验证函数，不调用它们会覆写历史报告的 main。"""
    started = time.monotonic()
    report = {"stage": stage, "status": "failed", "validation": "current live smoke" if stage == "live_smoke" else "fixed model / actual local components"}
    try:
        if stage in {"core_unit", "protocol_cli"}:
            patterns = CORE_TEST_PATTERNS if stage == "core_unit" else TEST_PATTERNS
            loader, suite = unittest.TestLoader(), unittest.TestSuite()
            for pattern in patterns:
                suite.addTests(loader.discover(str(ROOT / "tests"), pattern=pattern))
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            report.update(status="passed" if result.wasSuccessful() and result.testsRun > 0 and not result.skipped else "failed",
                tests_run=result.testsRun, failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped), patterns=list(patterns))
        else:
            results = []
            for target in STAGES[stage]:
                module, function = target.split(":")
                value = getattr(importlib.import_module(module), function)()
                passed = value.get("passed") is True if stage == "live_smoke" else value.get("status") == "passed"
                results.append({"target": target, "status": "passed" if passed else "failed", "result": value})
            report.update(status="passed" if results and all(row["status"] == "passed" for row in results) else "failed", results=results)
    except KeyboardInterrupt:
        report["status"] = "cancelled"
    except Exception as error:
        report.update(error_type=type(error).__name__, stack=traceback.format_tb(error.__traceback__))
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    write_json(result_path, report)
    return 0 if report["status"] == "passed" else 130 if report["status"] == "cancelled" else 1


def stop_process(process):
    """回收当前验收 worker 及其子进程；不操作任何已有用户进程。"""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=15)
    else:
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=15)


def run_stage(name, directory, *, command=None, timeout=180):
    result_path, log_path = directory / f"{name}.json", directory / f"{name}.log"
    command = command or [sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()), "--worker", name, "--result", str(result_path)]
    row = {"stage": name, "status": "failed", "detail": result_path.name, "log": log_path.name}
    started, process = time.monotonic(), None
    try:
        with log_path.open("wb") as log:
            process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}))
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                row["error_type"] = "StageTimeout"
                stop_process(process)
            except KeyboardInterrupt:
                row.update(status="cancelled", error_type="KeyboardInterrupt")
                stop_process(process)
        row["returncode"] = process.returncode
        if result_path.exists():
            result = json.loads(result_path.read_text(encoding="utf-8"))
            row["worker_status"] = result.get("status")
            if "error_type" not in row:
                row["status"] = "passed" if process.returncode == 0 and result.get("stage") == name and result.get("status") == "passed" else (
                    "cancelled" if process.returncode == 130 or result.get("status") == "cancelled" else "failed")
            if "tests_run" in result:
                row["tests_run"] = result["tests_run"]
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        row.update(status="failed", error_type=type(error).__name__)
        if process is not None and process.poll() is None:
            stop_process(process)
    row["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return row


def report_status(stages, evidence, *, include_live, require_history=False):
    required = [row for row in stages if include_live or row["stage"] != "live_smoke"]
    if any(row["status"] == "cancelled" for row in required):
        return "cancelled"
    expected = set(DEFAULT_STAGES) | ({"live_smoke"} if include_live else set())
    complete = len(required) == len(expected) and {row["stage"] for row in required} == expected
    history_ok = evidence["status"] == "passed" if require_history else True
    return "passed" if complete and all(row["status"] == "passed" for row in required) and history_ok else "failed"


def publish(directory, report):
    write_json(directory / "summary.json", report)
    lines = ["# Personal Agent 当前环境基础验收", "", f"结果：{report['status']}。目标环境兼容性由独立验收记录说明。", "",
        "| 本轮阶段 | 状态 | 明细 | 日志 |", "| --- | --- | --- | --- |"]
    for row in report["stages"]:
        lines.append(f"| {row['stage']} | {row['status']} | [{row.get('detail', '未运行')}]({row.get('detail', 'summary.json')}) | [{row.get('log', '无')}]({row.get('log', 'summary.json')}) |")
    lines.extend(["", "本轮固定模型/本地组件与历史真实网络证据分别记录。历史文件存在不代表当前重新运行通过。", "",
        "历史记录检查：" + report.get("historical_evidence", {}).get("status", "not_recorded") + "；仅 --history 显式启用时影响总结果。", "",
        "此本地验收不替代原生服务兼容或实际部署环境验收；容器结果见独立工作流。", "",
        "没有广泛质量基准或泛化提升结论；默认只运行选定主链路和定向测试。"])
    (directory / "overview.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_acceptance(directory, *, include_live=False, check_history=False):
    report = {"version": 1, "started_at": datetime.now(timezone.utc).isoformat(), "status": "running", "directory": str(directory),
        "source": source_state(), "environment": environment_state(),
        "historical_evidence": historical_evidence() if check_history else {"status": "not_requested", "boundary": "historical files are not required for offline local acceptance"},
        "history_required": check_history, "stages": [],
        "m3": {"complete": False, "remaining": ["anthropic_remote_and_signatures", "container_build_run_volumes_signals", "recorded_behavior_differences"],
            "quality_boundary": "authored demo tasks, not a broad quality benchmark"}}
    cancelled = False
    publish(directory, report)
    for name in DEFAULT_STAGES:
        if cancelled:
            row = {"stage": name, "status": "not_run", "reason": "earlier cancellation"}
        else:
            print(f"[{name}] running", flush=True)
            row = run_stage(name, directory)
            print(f"[{name}] {row['status']}", flush=True)
        report["stages"].append(row)
        cancelled = cancelled or row["status"] == "cancelled"
        publish(directory, report)
    local_passed = all(row["status"] == "passed" for row in report["stages"])
    if include_live and local_passed:
        print("[live_smoke] running (real model requests)", flush=True)
        live = run_stage("live_smoke", directory)
        print(f"[live_smoke] {live['status']}", flush=True)
    else:
        live = {"stage": "live_smoke", "status": "not_run", "reason": "not requested" if not include_live else "local checks did not pass"}
    report["stages"].append(live)
    report["status"] = report_status(report["stages"], report["historical_evidence"], include_live=include_live, require_history=check_history)
    after = source_state()
    report["source_unchanged_during_run"] = after["sha256"] == report["source"]["sha256"]
    if not report["source_unchanged_during_run"] and report["status"] != "cancelled":
        report.update(status="failed", error_type="SourceChangedDuringRun")
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["m3"]["local_baseline_status"] = "passed" if local_passed else "cancelled" if cancelled else "failed"
    publish(directory, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="本地主链路通过后，额外运行一次当前模型 explore/读取/MCP 冒烟；会产生实际用量")
    parser.add_argument("--history", action="store_true", help="额外检查本机历史报告；缺失或失败时总结果失败，不会重新请求历史模型")
    parser.add_argument("--output-dir", type=Path, help="新报告目录；必须尚不存在，相对路径按调用方目录解析")
    parser.add_argument("--worker", choices=tuple(STAGES), help=argparse.SUPPRESS)
    parser.add_argument("--result", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not __debug__:
        parser.error("验收依赖已有断言，请勿使用 -O 或 -OO。")
    if args.worker:
        if args.result is None:
            parser.error("内部 worker 需要 --result。")
        raise SystemExit(worker(args.worker, args.result))
    if args.result is not None:
        parser.error("--result 仅用于内部 worker。")
    directory = (args.output_dir or ROOT / "codex/acceptance-runs" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8])).resolve()
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except OSError:
        parser.error("报告目录不能创建或已存在；请选择新的 --output-dir。")
    try:
        report = run_acceptance(directory, include_live=args.live, check_history=args.history)
    except KeyboardInterrupt:
        summary = directory / "summary.json"
        report = json.loads(summary.read_text(encoding="utf-8")) if summary.exists() else {"version": 1, "stages": [], "m3": {"complete": False}}
        report.update(status="cancelled", finished_at=datetime.now(timezone.utc).isoformat())
        report["m3"]["local_baseline_status"] = "cancelled"
        recorded = {row["stage"] for row in report["stages"]}
        missing = [name for name in (*DEFAULT_STAGES, "live_smoke") if name not in recorded]
        report["stages"].extend({"stage": name, "status": "cancelled" if index == 0 else "not_run", "reason": "controller interrupted"} for index, name in enumerate(missing))
        publish(directory, report)
    print(json.dumps({"status": report["status"], "summary": str(directory / "summary.json"), "overview": str(directory / "overview.md"), "m3_complete": False}, ensure_ascii=False))
    raise SystemExit(0 if report["status"] == "passed" else 130 if report["status"] == "cancelled" else 1)


if __name__ == "__main__":
    main()
