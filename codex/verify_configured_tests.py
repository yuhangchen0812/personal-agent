"""外部目录测试配置与复验的两条主流程；固定模型，实际 pytest/Windows Job/CLI。"""

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex.verify_delivery import isolated, message
from personal_agent.ci_diagnosis import run_ci_diagnosis
from personal_agent.ci_repair import apply_repair
from personal_agent.configured_tests import load_test_config, run_project_tests
from personal_agent.configured_verification import verify_configured_proposal
from personal_agent.diagnosis_contract import RUN, FAILURES
from personal_agent.file_runtime import write_json_atomic
from personal_agent.repair_verification import load_checked_verification
from personal_agent.source_snapshot import PREFIX, snapshot_sources


EXAMPLE = ROOT / "examples/configured-python"
NOTE = "已审阅自编项目、测试和候选差异；仅在本地副本执行配置限定的 pytest，演示后恢复原字节。"


def prepare(root):
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(EXAMPLE / "project", root / "outside project")
    shutil.copyfile(EXAMPLE / "test-config.json", root / "测试配置.json")
    return root / "outside project", root / "测试配置.json"


def fixed_model(messages, definitions, *, on_usage, **_):
    on_usage({"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3})
    if len(messages) == 2:
        return message(("run", "read_file", {"file_path": RUN}), ("failures", "read_file", {"file_path": FAILURES}),
            ("code", "read_file", {"file_path": PREFIX + "billing/fees.py"}),
            ("tests", "read_file", {"file_path": PREFIX + "tests/test_fees.py"}))
    results = {item["tool_call_id"]: item["content"] for item in messages if item["role"] == "tool"}
    def lines(identity):
        return [(int(match[1]), match[2]) for match in re.finditer(r"(?m)^\s*(\d+) \| (.*)$", results[identity])]
    test = json.loads(lines("failures")[0][1])["test"]
    number = next(number for number, text in lines("code") if "if subtotal > 100:" in text)
    source = {"path": PREFIX + "billing/fees.py", "start_line": number, "end_line": number}
    return {"role": "assistant", "content": json.dumps({"summary": "满额边界需要包含等号，等待复验。",
        "failures": [{"test": test, "status": "suspected", "missing_information": [], "hypothesis": "阈值比较排除了恰好满额。", "recommendation": "最小修改后复验。",
            "evidence": [{"path": FAILURES, "start_line": 1, "end_line": 1}, source]}],
        "edits": [{"path": PREFIX + "billing/fees.py", "old": "if subtotal > 100:", "new": "if subtotal >= 100:",
            "addresses": [test], "evidence": [source]}], "limitations": ["固定模型响应，不是模型质量证据。"]}, ensure_ascii=False)}


def diagnosis_fixture(root):
    source, config = prepare(root)
    baseline = run_project_tests(source, config, NOTE, output_dir=root / "initial tests")
    assert baseline["status"] == "tests_failed" and baseline["summary"]["total"] == 4
    with isolated(root), patch("personal_agent.task_runner.ROOT", root):
        diagnosis = run_ci_diagnosis(Path(baseline["directory"]) / "run.xml", source, output_mode="legacy", call_model=fixed_model)
    assert diagnosis["status"] == "evidence_checked"
    return source, config, Path(diagnosis["directory"]) / "report.json", baseline


def cli(root, arguments):
    process = subprocess.run([sys.executable, "-X", "utf8", "-B", str(ROOT / "run_task.py"), "repair", *arguments],
        cwd=root, capture_output=True, timeout=40)
    assert process.returncode in (0, 1, 2), process.stderr.decode("utf-8", errors="replace")
    return process.returncode, json.loads(process.stdout) if process.stdout else None


def verify_workflow():
    with tempfile.TemporaryDirectory(prefix="configured-中文 空格-") as temporary:
        root = Path(temporary).resolve()
        source, config, diagnosis, baseline = diagnosis_fixture(root)
        before = snapshot_sources(source)["files"]
        # 新入口实际经 CLI；cwd 和配置/诊断/输出均在项目之外，参数含中文空格。
        code, receipt = cli(root, ["verify", "--diagnosis", str(diagnosis.relative_to(root)), "--test-config", config.name,
            "--review-note", NOTE, "--output-dir", "新复验"])
        assert code == 0 and receipt["status"] == "verified_with_test_config", receipt
        receipt_path = root / "新复验/receipt.json"
        load_checked_verification(receipt_path)
        code, adoption = cli(root, ["apply", "--verification", "新复验/receipt.json", "--review-note", NOTE, "--output-dir", "采纳记录"])
        assert code == 0 and adoption["status"] == "applied"
        code, passed = cli(root, ["test", "--source-dir", source.name, "--test-config", config.name, "--review-note", NOTE, "--output-dir", "采纳后测试"])
        assert code == 0 and passed["summary"]["passed"] == 4
        code, rollback = cli(root, ["rollback", "--repair-dir", "采纳记录", "--review-note", "恢复演示故障基线。"])
        assert code == 0 and rollback["status"] == "rolled_back" and snapshot_sources(source)["files"] == before
        # 只改配置就使旧回执过期，不能直接复用已通过的布尔字段。
        settings = json.loads(config.read_text(encoding="utf-8")); settings["timeout_seconds"] += 1
        write_json_atomic(config, settings)
        reject(lambda: apply_repair(receipt_path, NOTE, output_dir=root / "stale"))
        return {"status": "passed", "baseline": baseline["summary"], "candidate": receipt["summary"],
            "same_test_config_baseline_reproduction": True, "external_cwd_cli_apply_test_and_byte_rollback": True,
            "changed_configuration_blocks_adoption": True, "new_model_calls": 0, "model": "fixed"}


def reject(action):
    try:
        action()
    except (ValueError, OSError):
        return
    raise AssertionError("unsupported configuration or stale record accepted")


def process_alive(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        assert kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == 259
    finally:
        kernel.CloseHandle(handle)


def verify_guards():
    with tempfile.TemporaryDirectory(prefix="configured-guards-") as temporary:
        root = Path(temporary).resolve()
        source, config, diagnosis, _ = diagnosis_fixture(root / "coverage")
        raw = config.read_bytes()
        settings = json.loads(raw)
        settings["test_paths"] = ["tests/test_fees.py"]
        write_json_atomic(config, settings)
        incomplete = verify_configured_proposal(diagnosis, config, NOTE, output_dir=root / "incomplete")
        assert incomplete["status"] == "baseline_mismatch" and set(incomplete["runs"]) == {"baseline"}
        reject(lambda: apply_repair(root / "incomplete/receipt.json", NOTE, output_dir=root / "bad-apply"))
        for changes in ({"command": "echo bypass"}, {"test_paths": ["../project"]}, {"test_paths": ["--help"]}, {"timeout_seconds": True}):
            write_json_atomic(config, json.loads(raw) | changes)
            reject(lambda: load_test_config(config, snapshot_sources(source)["manifest"]))
        config.write_bytes(raw)
        # 实际进程探针：环境凭据不继承；正常退出也回收留下的子进程；超时亦回收。
        probe = root / "probe"
        probe.mkdir()
        target = probe / "test_probe.py"
        spawn = "import os, subprocess, sys, pathlib, time\n\ndef test_probe():\n    assert 'PERSONAL_AGENT_TEST_SECRET' not in os.environ\n    assert 'DASHSCOPE_API_KEY' not in os.environ\n    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n    pathlib.Path(os.environ['TEMP'], 'child.pid').write_text(str(child.pid))\n"
        probe_config = root / "probe.json"
        data = {"version": 1, "runner": "pytest", "test_paths": ["test_probe.py"], "timeout_seconds": 5, "max_output_bytes": 4096}
        write_json_atomic(probe_config, data)
        for label in ("normal", "timeout", "output"):
            text = spawn + ("    time.sleep(30)\n" if label == "timeout" else "    print('x' * 5000, flush=True)\n    time.sleep(30)\n" if label == "output" else "")
            target.write_text(text, encoding="utf-8")
            write_json_atomic(probe_config, data | {"timeout_seconds": 2 if label == "timeout" else 5})
            with patch.dict(os.environ, {"PERSONAL_AGENT_TEST_SECRET": "fake-sensitive-value"}):
                result = run_project_tests(probe, probe_config, NOTE, output_dir=root / label)
            pid = int((root / label / "run-runtime/tmp/child.pid").read_text())
            assert not process_alive(pid), f"child survived {label}"
            assert result["pytest"]["status"] == {"normal": "completed", "timeout": "timeout", "output": "output_limit"}[label], result
            assert Path(result["pytest"]["log"]).stat().st_size <= 4096
        return {"status": "passed", "omitted_tests_blocked_before_candidate_execution": True,
            "shell_options_traversal_and_bad_config_rejected": True, "environment_secret_not_inherited": True,
            "normal_exit_and_timeout_and_output_limit_kill_descendants": True,
            "bounded_logs": True, "new_model_calls": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or ROOT / "codex/acceptance-runs" / f"configured-tests-{uuid4().hex}.json"
    if output.exists():
        parser.error("输出已存在，未覆盖。")
    report = {"workflow": verify_workflow(), "guards": verify_guards(), "status": "passed"}
    write_json_atomic(output, report)
    print(json.dumps({"status": report["status"], "output": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
