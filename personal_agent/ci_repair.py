"""显式采纳已复验候选，保存原字节和事务记录，并按版本回滚；不调用模型。"""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from uuid import uuid4

from .file_runtime import plain_path, write_json_atomic
from .repair_verification import load_checked_verification, review_text, verify_demo_proposal
from .source_snapshot import PREFIX, digest, snapshot_sources
from .skill_task_eval import _fixture_name
from .task_runner import ROOT


@contextmanager
def _source_lock(source):
    """同一源码的本工具进程互斥；OS 在进程退出时释放，不阻止外部编辑器。"""
    key = digest(os.path.normcase(str(source)).encode("utf-8"))
    path = Path(tempfile.gettempdir()) / f"personal-agent-repair-{key}.lock"
    with path.open("a+b") as lock:
        lock.seek(0, 2)
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            acquire = lambda: msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            acquire = lambda: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(lock, fcntl.LOCK_UN)
        try:
            acquire()
        except OSError:
            raise ValueError("该源码正在执行其他修复操作，请稍后重试。") from None
        try:
            yield
        finally:
            release()


def _replace_checked(path, expected, replacement):
    path = plain_path(path)
    if path.read_bytes() != expected:
        raise ValueError(f"文件版本变化，未覆盖：{path}")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".repair-", suffix=".tmp", delete=False) as target:
            temporary = Path(target.name)
            target.write(replacement)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        if plain_path(path).read_bytes() != expected:
            raise ValueError(f"写入前文件版本变化，未覆盖：{path}")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _save(directory, state, status, **extra):
    state.update(status=status, **extra)
    write_json_atomic(directory / "state.json", state)


def _load(directory):
    directory = plain_path(directory)
    state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
    raw = (directory / "plan.json").read_bytes()
    plan = json.loads(raw)
    if (state.get("kind") != "ci_repair" or state.get("version") != 1 or state.get("directory") != str(directory)
            or digest(raw) != state.get("plan_sha256") or plan.get("version") != 1 or plan.get("directory") != str(directory)):
        raise ValueError("修复事务记录损坏或位置变化。")
    source = plain_path(plan["source_dir"])
    if source.is_relative_to(directory) or directory.is_relative_to(source):
        raise ValueError("事务备份不能与原源码嵌套。")
    before, after = {}, {}
    if set(plan["before_manifest"]) != set(plan["after_manifest"]):
        raise ValueError("事务不能增删源码文件。")
    for relative in plan["before_manifest"]:
        _fixture_name(PREFIX + relative)
        if not relative or Path(relative).is_absolute() or "\\" in relative or ".." in Path(relative).parts:
            raise ValueError("备份路径无效。")
        for label, result in (("before", before), ("after", after)):
            path = plain_path(directory / label / relative)
            if not path.is_relative_to(directory / label):
                raise ValueError("备份路径越界。")
            raw = path.read_bytes()
            if plan[label + "_manifest"][relative] != {"sha256": digest(raw), "bytes": len(raw)}:
                raise ValueError("事务备份字节损坏。")
            result[relative] = raw
    if plan["changed"] != sorted(name for name in before if before[name] != after[name]) or not plan["changed"]:
        raise ValueError("事务变更清单不一致。")
    return directory, source, state, plan, before, after


def _current_known(source, before, after):
    current = snapshot_sources(source)["files"]
    if set(current) != set(before) or any(raw not in (before[name], after[name]) for name, raw in current.items()):
        raise ValueError("源码出现事务之外的版本或文件变动，未覆盖；请先保留并处理这些变动。")
    return current


def _restore(directory, source, state, plan, before, after, note, *, failure=False):
    current = _current_known(source, before, after)
    # 回滚前一次性预检全部文件，避免发现后续文件已被用户编辑时先覆盖前面的文件。
    if state["status"] in {"rolled_back", "apply_failed_restored", "cancelled_restored"}:
        if current != before:
            raise ValueError("回滚完成后的源码再次变化，未执行重复回滚。")
        return state
    _save(directory, state, "rolling_back", rollback_note=note, source_matches_verified_candidate=False)
    for name in reversed(plan["changed"]):
        raw = current[name]
        if raw != before[name]:
            _replace_checked(source / name, raw, before[name])
    if snapshot_sources(source)["files"] != before:
        raise ValueError("回滚后源码版本不匹配。")
    status = ("cancelled_restored" if state.get("error_type") == "KeyboardInterrupt" else "apply_failed_restored") if failure else "rolled_back"
    _save(directory, state, status, source_restored=True, source_matches_verified_candidate=False)
    return state


def apply_repair(verification, review_note, *, output_dir=None, accept_partial=False):
    note = review_text(review_note)
    receipt_path = plain_path(verification)
    report, receipt = load_checked_verification(receipt_path)
    if not receipt["all_failures_fixed"] and not accept_partial:
        raise ValueError("候选仅修复部分失败；确认保留剩余失败后需显式 --accept-partial。")
    source = plain_path(report["source_dir"])
    directory = plain_path(output_dir if output_dir is not None else ROOT / ".personal/ci-repairs" / uuid4().hex)
    protected = (source, Path(report["directory"]), receipt_path.parent)
    if receipt.get("version") == 4:
        run_dir = Path(receipt["source_run"])
        inputs = json.loads((run_dir / "inputs.lock.json").read_text(encoding="utf-8"))
        protected += (run_dir, Path(receipt["evaluation_report"]).parent, Path(inputs["suite_source"]).parent)
    if directory.exists() or any(directory.is_relative_to(path) or path.is_relative_to(directory) for path in protected):
        raise ValueError("修复输出须为与源码、诊断和复验不嵌套的新目录。")
    with _source_lock(source):
        # 锁内重新检查复验、来源和候选，避免另一个修复进程使用同一旧版本。
        report, receipt = load_checked_verification(receipt_path)
        if not receipt["all_failures_fixed"] and not accept_partial:
            raise ValueError("复验不是全部修复，需显式 --accept-partial。")
        before = snapshot_sources(source)
        after = snapshot_sources(Path(report["directory"]) / "candidate-source")
        changed = sorted(name for name in before["files"] if before["files"][name] != after["files"][name])
        if not changed:
            raise ValueError("候选没有可采纳的字节变更。")
        directory.mkdir(parents=True, exist_ok=False)
        for label, snapshot in (("before", before), ("after", after)):
            for name, raw in snapshot["files"].items():
                target = directory / label / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
        plan = {"version": 1, "directory": str(directory), "source_dir": str(source), "review_note": note,
            "verification": str(receipt_path), "verification_sha256": digest(receipt_path.read_bytes()),
            "diagnosis_report": receipt["diagnosis_report"], "diagnosis_sha256": receipt["diagnosis_sha256"],
            "before_manifest": before["manifest"], "after_manifest": after["manifest"], "changed": changed,
            "all_failures_fixed": receipt["all_failures_fixed"], "remaining_unaddressed": receipt["remaining_unaddressed"],
            "accept_partial": bool(accept_partial), "verification_status": receipt["status"]}
        if receipt.get("version") == 4:
            plan["regression_gate"] = {key: receipt[key] for key in ("case_id", "evaluation_report", "evaluation_sha256", "outcome", "remaining_regression_failures")}
        write_json_atomic(directory / "plan.json", plan)
        state = {"version": 1, "kind": "ci_repair", "directory": str(directory),
            "plan_sha256": digest((directory / "plan.json").read_bytes()), "new_model_calls": 0,
            "candidate_all_failures_fixed": receipt["all_failures_fixed"], "remaining_unaddressed": receipt["remaining_unaddressed"],
            "boundary": "explicit local adoption of reviewed candidate; per-file atomic replacement; no remote deployment or post-apply test claim"}
        if "regression_gate" in plan:
            state["regression_gate"] = plan["regression_gate"]
        _save(directory, state, "prepared")
        try:
            # 最后一次完整来源检查通过后才开始写入。
            load_checked_verification(receipt_path)
            if digest(receipt_path.read_bytes()) != plan["verification_sha256"]:
                raise ValueError("采纳准备期间复验回执变化。")
            _save(directory, state, "applying")
            for name in changed:
                _replace_checked(source / name, before["files"][name], after["files"][name])
            if snapshot_sources(source)["manifest"] != after["manifest"]:
                raise ValueError("采纳后源码版本不匹配。")
            _save(directory, state, "applied" if receipt["all_failures_fixed"] else "applied_partial", source_matches_verified_candidate=True)
        except BaseException as error:
            state["error_type"] = type(error).__name__
            try:
                _restore(directory, source, state, plan, before["files"], after["files"], "采纳异常后自动恢复原字节。", failure=True)
            except BaseException as restore_error:
                _save(directory, state, "recovery_required", recovery_error_type=type(restore_error).__name__)
        return state


def rollback_repair(directory, review_note):
    note = review_text(review_note)
    directory, source, state, plan, before, after = _load(directory)
    with _source_lock(source):
        directory, source, state, plan, before, after = _load(directory)
        return _restore(directory, source, state, plan, before, after, note)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    test = sub.add_parser("test", help="按显式配置在受限副本运行经过审阅的本地 Python 项目")
    test.add_argument("--source-dir", required=True, type=Path)
    test.add_argument("--test-config", required=True, type=Path)
    test.add_argument("--review-note", required=True)
    test.add_argument("--output-dir", required=True, type=Path)
    verify = sub.add_parser("verify", help="审阅后复验候选；外部项目须显式提供 --test-config")
    verify.add_argument("--diagnosis", required=True, type=Path)
    verify.add_argument("--review-note", required=True)
    verify.add_argument("--output-dir", type=Path)
    verify.add_argument("--test-config", type=Path)
    apply = sub.add_parser("apply", help="显式采纳已复验候选到诊断绑定的原源码目录")
    apply.add_argument("--verification", required=True, type=Path)
    apply.add_argument("--review-note", required=True)
    apply.add_argument("--accept-partial", action="store_true")
    apply.add_argument("--output-dir", type=Path)
    rollback = sub.add_parser("rollback", help="按事务备份回滚；外部新变动将阻止覆盖")
    rollback.add_argument("--repair-dir", required=True, type=Path)
    rollback.add_argument("--review-note", required=True)
    options = vars(parser.parse_args(argv))
    action, note = options.pop("action"), options.pop("review_note")
    try:
        if action == "test":
            from .configured_tests import run_project_tests
            result = run_project_tests(options.pop("source_dir"), options.pop("test_config"), note, **options)
        elif action == "verify":
            config = options.pop("test_config")
            if config is None:
                result = verify_demo_proposal(options.pop("diagnosis"), note, **options)
            else:
                from .configured_verification import verify_configured_proposal
                result = verify_configured_proposal(options.pop("diagnosis"), config, note, **options)
        elif action == "apply":
            result = apply_repair(options.pop("verification"), note, **options)
        else:
            result = rollback_repair(options.pop("repair_dir"), note)
    except KeyboardInterrupt:
        print("修复操作已取消，请检查事务记录。", file=sys.stderr)
        raise SystemExit(130) from None
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"修复操作失败：{error}", file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps(result, ensure_ascii=False))
    successful = {"verified_on_demo", "partial_fix_verified_on_demo", "verified_with_test_config", "partial_fix_verified_with_test_config",
        "tests_passed", "applied", "applied_partial", "rolled_back"}
    already_restored = action == "rollback" and result["status"] in {"apply_failed_restored", "cancelled_restored"}
    raise SystemExit(0 if result["status"] in successful or already_restored else 130 if result.get("error_type") == "KeyboardInterrupt" else 1)
