"""Repository verification: fixed responses by default; --live explicitly requests a model."""

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
    "delivery": ("tests.support.verify_delivery:run_joint",),
    "memory_cache": ("tests.support.verify_runtime_acceptance:verify_joint",),
    "compaction": ("tests.support.verify_compaction_routes:verify_routes", "tests.support.verify_compaction_routes:verify_compatibility"),
    "delegation": ("tests.support.verify_delegation_delivery:run_suite",),
    "file_tasks": ("tests.support.verify_ci_tasks:verify_workflow", "tests.support.verify_ci_tasks:verify_failures"),
    "task_learning": ("tests.support.verify_task_learning:verify_workflow", "tests.support.verify_task_learning:verify_guards"),
    "ci_import": ("tests.support.verify_ci_import:verify_workflow", "tests.support.verify_ci_import:verify_guards"),
    "junit_ci": ("tests.support.verify_junit_ci:verify_workflow", "tests.support.verify_junit_ci:verify_guards"),
    "ci_diagnosis": ("tests.support.verify_ci_diagnosis:verify_workflow", "tests.support.verify_ci_diagnosis:verify_guards"),
    "ci_repair": ("tests.support.verify_ci_repair:verify_workflow", "tests.support.verify_ci_repair:verify_guards",
        "tests.support.verify_ci_repair:verify_recovery", "tests.support.verify_ci_repair:verify_cli"),
    "configured_tests": ("tests.support.verify_configured_tests:verify_workflow", "tests.support.verify_configured_tests:verify_guards"),
    "structured_diagnosis": ("tests.support.verify_structured_diagnosis:verify_workflow", "tests.support.verify_structured_diagnosis:verify_guards"),
    "repair_eval": ("tests.support.verify_repair_eval:verify_workflow", "tests.support.verify_repair_eval:verify_guards"),
    "eval_adoption": ("tests.support.verify_eval_adoption:verify_workflow", "tests.support.verify_eval_adoption:verify_guards"),
    "evidence_limits": ("tests.support.verify_evidence_limits:verify_workflow", "tests.support.verify_evidence_limits:verify_guards"),
    "rule_evidence": ("tests.support.verify_rule_evidence:verify_workflow", "tests.support.verify_rule_evidence:verify_guards"),
    "diagnosis_followup": ("tests.support.verify_diagnosis_followup:verify_workflow", "tests.support.verify_diagnosis_followup:verify_guards"),
    "diagnosis_history": ("tests.support.verify_diagnosis_history:verify_workflow",),
    "repair_workflow": ("tests.support.verify_repair_workflow:verify_workflow",),
    "project_import": ("tests.support.verify_project_import:verify_workflow",),
    "skill_learning": ("tests.support.verify_skill_learning:verify_workflow",),
    "delivery_bundle": ("tests.support.verify_delivery_bundle:verify_workflow",),
    "qwen_thinking": ("tests.support.verify_qwen_thinking:verify_workflow",),
    "qwen_messages": ("tests.support.verify_qwen_messages:verify_workflow",),
    "container_delivery": ("tests.support.verify_container_delivery:verify_workflow",),
    "release_delivery": ("tests.support.verify_release_delivery:verify_workflow",),
    "protocol_cli": (),
    "live_smoke": ("tests.support.verify_delivery:run_live",),
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


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_state():
    suffixes = {".py", ".json", ".jsonl", ".ini", ".md", ".toml", ".txt"}
    files = sorted(path for directory in ("personal_agent", "tests", "scripts", "examples")
        for path in (ROOT / directory).rglob("*")
        if path.is_file() and path.suffix in suffixes and "__pycache__" not in path.parts)
    files += [ROOT / name for name in ("requirements.txt", "requirements-dev.txt", "run.cmd", "run_agent.py",
        "verify.cmd", "task.cmd", "run_task.py", "demo.cmd", "run_demo.py", "Dockerfile", ".dockerignore")]
    files += [ROOT / "playground/README.md", ROOT / "playground/文件读取示例.txt"]
    files += sorted((ROOT / ".github/workflows").glob("*.yml"))
    files += sorted((ROOT / ".github/workflows").glob("*.yaml"))
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


def report_status(stages, *, include_live):
    required = [row for row in stages if include_live or row["stage"] != "live_smoke"]
    if any(row["status"] == "cancelled" for row in required):
        return "cancelled"
    expected = set(DEFAULT_STAGES) | ({"live_smoke"} if include_live else set())
    complete = len(required) == len(expected) and {row["stage"] for row in required} == expected
    return "passed" if complete and all(row["status"] == "passed" for row in required) else "failed"


def publish(directory, report):
    write_json(directory / "summary.json", report)
    lines = ["# Personal Agent 当前环境基础验收", "", f"结果：{report['status']}。目标环境兼容性由独立验收记录说明。", "",
        "| 本轮阶段 | 状态 | 明细 | 日志 |", "| --- | --- | --- | --- |"]
    for row in report["stages"]:
        lines.append(f"| {row['stage']} | {row['status']} | [{row.get('detail', '未运行')}]({row.get('detail', 'summary.json')}) | [{row.get('log', '无')}]({row.get('log', 'summary.json')}) |")
    lines.extend(["", "默认运行固定模型和真实本地组件；不请求模型服务。",
        "--live 额外执行显式的真实模型冒烟检查，可能产生用量。",
        "容器环境以独立工作流为准；流程通过不代表通用模型质量。"])
    (directory / "overview.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_acceptance(directory, *, include_live=False):
    report = {"version": 2, "started_at": datetime.now(timezone.utc).isoformat(), "status": "running", "directory": str(directory),
        "source": source_state(), "environment": environment_state(), "stages": []}
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
    report["status"] = report_status(report["stages"], include_live=include_live)
    after = source_state()
    report["source_unchanged_during_run"] = after["sha256"] == report["source"]["sha256"]
    if not report["source_unchanged_during_run"] and report["status"] != "cancelled":
        report.update(status="failed", error_type="SourceChangedDuringRun")
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["offline_status"] = "passed" if local_passed else "cancelled" if cancelled else "failed"
    publish(directory, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="本地主链路通过后，额外运行一次当前模型 explore/读取/MCP 冒烟；会产生实际用量")
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
    directory = (args.output_dir or ROOT / ".personal/verification" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8])).resolve()
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except OSError:
        parser.error("报告目录不能创建或已存在；请选择新的 --output-dir。")
    try:
        report = run_acceptance(directory, include_live=args.live)
    except KeyboardInterrupt:
        summary = directory / "summary.json"
        report = json.loads(summary.read_text(encoding="utf-8")) if summary.exists() else {"version": 2, "stages": []}
        report.update(status="cancelled", finished_at=datetime.now(timezone.utc).isoformat())
        report["offline_status"] = "cancelled"
        recorded = {row["stage"] for row in report["stages"]}
        missing = [name for name in (*DEFAULT_STAGES, "live_smoke") if name not in recorded]
        report["stages"].extend({"stage": name, "status": "cancelled" if index == 0 else "not_run", "reason": "controller interrupted"} for index, name in enumerate(missing))
        publish(directory, report)
    print(json.dumps({"status": report["status"], "summary": str(directory / "summary.json"), "overview": str(directory / "overview.md")}, ensure_ascii=False))
    raise SystemExit(0 if report["status"] == "passed" else 130 if report["status"] == "cancelled" else 1)


if __name__ == "__main__":
    main()
