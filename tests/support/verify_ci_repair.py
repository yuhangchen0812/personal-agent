"""受控修复的离线功能验收；真实 pytest/文件/启动器，诊断模型固定。"""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.support.verify_ci_diagnosis import fixed_model, setup
from tests.support.verify_delivery import isolated
from personal_agent import ci_repair
from personal_agent.ci_diagnosis import load_checked_diagnosis, run_ci_diagnosis
from personal_agent.file_runtime import write_json_atomic
from personal_agent.junit_import import parse_junit
from personal_agent.pytest_demo import DEMO, execute_pytest
from personal_agent.repair_verification import verify_demo_proposal
from personal_agent.source_snapshot import digest, snapshot_sources


NOTE = "已审阅实际差异：仅预期函数体改动，测试断言、导入、签名及配置保持；在已知演示副本复验。"


def fixture(root, mode="correct"):
    root.mkdir(parents=True, exist_ok=True)
    baseline = setup(root)
    with isolated(root), patch("personal_agent.task_runner.ROOT", root):
        result = run_ci_diagnosis(baseline["xml"], root / "broken-project", output_mode="legacy", call_model=fixed_model(mode))
    report_path = Path(result["directory"]) / "report.json"
    receipt = verify_demo_proposal(report_path, NOTE)
    return report_path, Path(receipt["directory"]) / "receipt.json"


def reject(action):
    try:
        action()
    except (ValueError, OSError):
        return
    raise AssertionError("unsupported or changed evidence accepted")


def verify_workflow():
    with tempfile.TemporaryDirectory(prefix="repair-workflow-") as temporary:
        root = Path(temporary).resolve()
        baseline = snapshot_sources(DEMO)["manifest"]
        diagnosis, receipt = fixture(root)
        source = root / "broken-project"
        before = snapshot_sources(source)["files"]
        report_raw, receipt_raw = diagnosis.read_bytes(), receipt.read_bytes()
        candidate = snapshot_sources(diagnosis.parent / "candidate-source")["files"]
        result = ci_repair.apply_repair(receipt, NOTE, output_dir=root / "repair")
        assert result["status"] == "applied" and snapshot_sources(source)["files"] == candidate
        # 实际在被采纳的源码上重新运行；这个结果独立于 apply 的版本匹配声明。
        applied_run = execute_pytest(source, root / "post-apply/result.xml")
        assert applied_run["returncode"] == 0
        rolled = ci_repair.rollback_repair(root / "repair", "演示结束，恢复原始故障基线。")
        assert rolled["status"] == "rolled_back" and snapshot_sources(source)["files"] == before
        repeated_raw = (root / "repair/state.json").read_bytes()
        assert ci_repair.rollback_repair(root / "repair", "重复调用不重复写入。") == rolled
        assert (root / "repair/state.json").read_bytes() == repeated_raw
        restored_run = execute_pytest(source, root / "post-rollback/result.xml")
        assert restored_run["returncode"] == 1
        # 同一已复验候选可在恢复后的同一版本显式再次采纳，使用全新事务。
        assert ci_repair.apply_repair(receipt, NOTE, output_dir=root / "repair-again")["status"] == "applied"
        ci_repair.rollback_repair(root / "repair-again", "恢复基线。")
        assert diagnosis.read_bytes() == report_raw and receipt.read_bytes() == receipt_raw
        assert snapshot_sources(DEMO)["manifest"] == baseline
        return {"status": "passed", "apply_then_actual_pytest_exit": 0, "rollback_then_actual_pytest_exit": 1,
            "original_bytes_restored": True, "idempotent_rollback_and_new_explicit_transaction": True,
            "historical_evidence_unchanged": True, "new_model_calls": 0, "diagnosis_model": "fixed"}


def verify_guards():
    with tempfile.TemporaryDirectory(prefix="repair-guards-") as temporary:
        root = Path(temporary).resolve()
        diagnosis, receipt = fixture(root, "partial")
        source = root / "broken-project"
        before = snapshot_sources(source)["files"]
        reject(lambda: ci_repair.apply_repair(receipt, NOTE, output_dir=root / "not-accepted"))
        assert not (root / "not-accepted").exists()
        reject(lambda: ci_repair.apply_repair(receipt, " ", accept_partial=True, output_dir=root / "blank"))
        reject(lambda: ci_repair.apply_repair(receipt, NOTE, accept_partial=True, output_dir=source / "nested"))
        reject(lambda: ci_repair.apply_repair(receipt, NOTE, accept_partial=True, output_dir=diagnosis.parent / "nested"))
        # 回执的布尔声明不能替代实际 XML；日志/源码/候选版本均要重新读取。
        receipt_raw = receipt.read_bytes()
        value = json.loads(receipt_raw)
        value["all_failures_fixed"] = True
        write_json_atomic(receipt, value)
        reject(lambda: ci_repair.apply_repair(receipt, NOTE, accept_partial=True, output_dir=root / "tampered"))
        receipt.write_bytes(receipt_raw)
        for target in (source / "checkout.py", diagnosis.parent / "candidate-source/checkout.py", receipt.parent / "candidate.log",
                       receipt.parent / "candidate.xml", receipt.parent / "source/demo_cases.py"):
            raw = target.read_bytes()
            target.write_bytes(raw + b"\n# changed\n")
            reject(lambda: ci_repair.apply_repair(receipt, NOTE, accept_partial=True, output_dir=root / "tampered"))
            target.write_bytes(raw)
        # 诊断摘要和原回复也必须一致，不能用 summary 内 edits 绕过复验覆盖。
        raw = diagnosis.read_bytes()
        value = json.loads(raw)
        value["diagnosis"]["edits"] = []
        write_json_atomic(diagnosis, value)
        reject(lambda: load_checked_diagnosis(diagnosis))
        diagnosis.write_bytes(raw)
        result = ci_repair.apply_repair(receipt, NOTE, accept_partial=True, output_dir=root / "partial")
        assert result["status"] == "applied_partial" and not result["candidate_all_failures_fixed"] and len(result["remaining_unaddressed"]) == 1
        run = execute_pytest(source, root / "partial-test/result.xml")
        assert run["returncode"] == 1
        patched = snapshot_sources(source)["files"]
        reject(lambda: ci_repair.apply_repair(receipt, NOTE, accept_partial=True, output_dir=root / "partial"))
        # 回滚预检所有文件，非改动文件的外部编辑也不能丢失。
        target = source / "demo_cases.py"
        raw = target.read_bytes()
        target.write_bytes(raw + b"\n# user work\n")
        changed = snapshot_sources(source)["files"]
        reject(lambda: ci_repair.rollback_repair(root / "partial", "不得覆盖新编辑。"))
        assert snapshot_sources(source)["files"] == changed
        target.write_bytes(raw)
        backup = root / "partial/before/checkout.py"
        raw_backup = backup.read_bytes()
        backup.write_bytes(raw_backup + b"# corrupt")
        reject(lambda: ci_repair.rollback_repair(root / "partial", "损坏备份不得使用。"))
        assert snapshot_sources(source)["files"] == patched
        backup.write_bytes(raw_backup)
        ci_repair.rollback_repair(root / "partial", "恢复原始字节。")
        assert snapshot_sources(source)["files"] == before
        return {"status": "passed", "partial_requires_explicit_acceptance": True, "partial_actual_pytest_exit": 1,
            "versions_receipt_xml_log_and_summary_rechecked": True, "foreign_edits_and_corrupt_backups_preserved": True,
            "output_overlap_and_existing_directory_rejected": True, "new_model_calls": 0}


def verify_recovery():
    with tempfile.TemporaryDirectory(prefix="repair-recovery-") as temporary:
        root = Path(temporary).resolve()
        diagnosis, receipt = fixture(root)
        source = root / "broken-project"
        before = snapshot_sources(source)["files"]
        replace = ci_repair._replace_checked
        for label, exception in (("io", OSError("injected second-file failure")), ("cancel", KeyboardInterrupt())):
            count = 0
            def fail_second(*args):
                nonlocal count
                count += 1
                if count == 2:
                    raise exception
                return replace(*args)
            with patch.object(ci_repair, "_replace_checked", fail_second):
                result = ci_repair.apply_repair(receipt, NOTE, output_dir=root / label)
            assert result["status"] == ("cancelled_restored" if label == "cancel" else "apply_failed_restored")
            assert snapshot_sources(source)["files"] == before
        # 最终回执写失败也不能悄悄留下已应用源码。
        failed = False
        def fail_final(path, value):
            nonlocal failed
            if value.get("status") == "applied" and not failed:
                failed = True
                raise OSError("injected receipt failure")
            return write_json_atomic(path, value)
        with patch.object(ci_repair, "write_json_atomic", fail_final):
            result = ci_repair.apply_repair(receipt, NOTE, output_dir=root / "receipt-failure")
        assert result["status"] == "apply_failed_restored" and snapshot_sources(source)["files"] == before
        # 模拟无法自动恢复的中断；后续进程可仅凭事务备份完成回滚，不依赖旧模型记录。
        count = 0
        def fail_and_block_restore(*args):
            nonlocal count
            count += 1
            if count >= 2:
                raise OSError("injected write and restore failure")
            return replace(*args)
        with patch.object(ci_repair, "_replace_checked", fail_and_block_restore):
            result = ci_repair.apply_repair(receipt, NOTE, output_dir=root / "resume")
        assert result["status"] == "recovery_required"
        command = [sys.executable, "-X", "utf8", "-B", str(ROOT / "run_task.py"), "repair", "rollback",
            "--repair-dir", str(root / "resume"), "--review-note", "新进程恢复未完成事务。"]
        process = subprocess.run(command, cwd=root, capture_output=True, timeout=30)
        assert process.returncode == 0, process.stderr.decode("utf-8")
        assert json.loads(process.stdout)["status"] == "rolled_back" and snapshot_sources(source)["files"] == before
        assert not list(source.glob(".repair-*.tmp"))
        return {"status": "passed", "second_file_io_and_interrupt_restore_original_bytes": True,
            "final_receipt_failure_restored": True, "unfinished_transaction_rolled_back_by_new_process": True,
            "temporary_files_cleaned": True, "new_model_calls": 0}


def verify_cli():
    with tempfile.TemporaryDirectory(prefix="repair-cli-中文 空格-") as temporary:
        root = Path(temporary).resolve()
        diagnosis, _ = fixture(root, "partial")
        def cli(arguments, shell="powershell"):
            launcher = str(ROOT / "task.cmd")
            if shell == "cmd":
                line = '"' + launcher + '" ' + subprocess.list2cmdline(arguments)
                command = 'cmd.exe /d /s /c "' + line + '"'
            else:
                quote = lambda value: "'" + value.replace("'", "''") + "'"
                command = ["powershell.exe", "-NoProfile", "-Command", "& " + " ".join(quote(item) for item in [launcher, *arguments]) + "; exit $LASTEXITCODE"]
            process = subprocess.run(command, cwd=root, capture_output=True, timeout=30)
            return process.returncode, process.stdout.decode("utf-8"), process.stderr.decode("utf-8")
        relative = str(diagnosis.relative_to(root))
        code, output, error = cli(["repair", "verify", "--diagnosis", relative, "--review-note", "审阅折扣差异，保留未处理错误。", "--output-dir", "新复验"])
        assert code == 0, error
        assert json.loads(output)["status"] == "partial_fix_verified_on_demo"
        receipt = "新复验/receipt.json"
        args = ["repair", "apply", "--verification", receipt, "--review-note", "明确采纳已审阅的部分候选。", "--output-dir", "采纳记录"]
        assert cli(args)[0] == 2
        code, output, error = cli([*args, "--accept-partial"], "cmd")
        assert code == 0, error
        assert json.loads(output)["status"] == "applied_partial"
        assert cli([*args, "--accept-partial"])[0] == 2
        code, output, error = cli(["repair", "rollback", "--repair-dir", "采纳记录", "--review-note", "恢复原始故障基线。"])
        assert code == 0 and json.loads(output)["status"] == "rolled_back", error
        assert cli(["repair", "--help"], "cmd")[0] == 0
        assert cli(["repair", "apply", "--verification", receipt])[0] == 2
        return {"status": "passed", "cmd_and_powershell_external_cwd_relative_chinese_paths": True,
            "verify_apply_partial_rollback_help_and_failure_exit_codes": True, "new_model_calls": 0}
