"""显式 pytest 配置和受限本地执行；只复制现有源码快照，不安装依赖。"""

from importlib.metadata import version
import json
import os
from pathlib import Path, PurePosixPath
import sys

from .file_runtime import plain_path, write_json_atomic
from .junit_import import load_junit_report
from .restricted_process import run_limited
from .source_snapshot import digest, snapshot_sources


HARNESS = """import sys
if sys.stdin.buffer.read(1) != b'1':
    raise SystemExit(125)
import pytest
root = sys.argv.pop(1)
sys.path.insert(0, root)
raise SystemExit(pytest.main(sys.argv[1:]))
"""
RUNNER_INI = b"[pytest]\njunit_family = xunit2\njunit_suite_name = pytest\n"


def _pairs(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("测试配置不接受重复字段。")
        result[name] = value
    return result


def parse_test_config(raw, manifest):
    if len(raw) > 16384:
        raise ValueError("测试配置超过 16 KiB。")
    value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_pairs)
    if (not isinstance(value, dict) or set(value) != {"version", "runner", "test_paths", "timeout_seconds", "max_output_bytes"}
            or type(value["version"]) is not int or value["version"] != 1 or value["runner"] != "pytest"
            or not isinstance(value["test_paths"], list) or not 1 <= len(value["test_paths"]) <= 10
            or type(value["timeout_seconds"]) is not int or not 1 <= value["timeout_seconds"] <= 120
            or type(value["max_output_bytes"]) is not int or not 4096 <= value["max_output_bytes"] <= 1024 * 1024):
        raise ValueError("测试配置字段或范围无效；仅支持固定 pytest 执行器。")
    selected = []
    for name in value["test_paths"]:
        if (not isinstance(name, str) or not name or any(char in name for char in "\\:\x00\r\n")
                or PurePosixPath(name).is_absolute() or PurePosixPath(name).as_posix() != name
                or any(part.startswith((".", "-")) for part in PurePosixPath(name).parts)
                or not any(path == name and path.endswith(".py") or path.startswith(name + "/") and path.endswith(".py") for path in manifest)):
            raise ValueError("test_paths 须为冻结源码内的相对 Python 文件或目录，不接受选项/节点表达式。")
        if name.casefold() in selected:
            raise ValueError("test_paths 不接受重复路径。")
        selected.append(name.casefold())
    return value


def load_test_config(path, manifest):
    path = plain_path(path)
    with path.open("rb") as source:
        raw = source.read(16385)
    return raw, parse_test_config(raw, manifest), {"path": str(path), "sha256": digest(raw), "bytes": len(raw)}


def copy_snapshot(snapshot, target):
    for name, raw in snapshot["files"].items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)


def test_command(source, directory, label, settings):
    return [sys.executable, "-I", "-X", "utf8", "-B", "-c", HARNESS, str(source),
        "-q", "-s", "-c", str(directory / "runner.ini"), "--rootdir", str(source), "-p", "no:cacheprovider",
        "--tb=short", "--color=no", "--import-mode=prepend", f"--junitxml={directory / (label + '.xml')}", *settings["test_paths"]]


def execute_configured_tests(source, directory, label, settings):
    runtime = directory / (label + "-runtime")
    environment = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE") if name in os.environ}
    environment.update(PATH=str(Path(sys.executable).parent) + os.pathsep + str(Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "System32"),
        TEMP=str(runtime / "tmp"), TMP=str(runtime / "tmp"), HOME=str(runtime / "home"), USERPROFILE=str(runtime / "home"),
        APPDATA=str(runtime / "home"), LOCALAPPDATA=str(runtime / "home"), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTEST_ADDOPTS="")
    command = test_command(source, directory, label, settings)
    run = run_limited(command, cwd=source, environment=environment, runtime=runtime,
        timeout=settings["timeout_seconds"], max_output=settings["max_output_bytes"], xml_path=directory / (label + ".xml"))
    run.update(pytest_version=version("pytest"), executed_manifest=snapshot_sources(source)["manifest"])
    return run


def run_project_tests(source_dir, test_config, review_note, *, output_dir):
    from .repair_verification import review_text
    note = review_text(review_note)
    source = snapshot_sources(source_dir)
    raw_config, settings, config_source = load_test_config(test_config, source["manifest"])
    directory = plain_path(output_dir)
    if directory.exists() or directory.is_relative_to(source["root"]) or source["root"].is_relative_to(directory):
        raise ValueError("测试输出须为与原源码不嵌套的新目录。")
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "test-config.json").write_bytes(raw_config)
    (directory / "runner.ini").write_bytes(RUNNER_INI)
    copy_snapshot(source, directory / "source")
    report = {"version": 1, "kind": "ci_configured_test", "status": "running", "directory": str(directory),
        "review_note": note, "source_dir": str(source["root"]), "source_manifest": source["manifest"],
        "test_config_source": config_source, "settings": settings, "new_model_calls": 0}
    write_json_atomic(directory / "report.json", report)
    try:
        run = execute_configured_tests(directory / "source", directory, "run", settings)
        report["pytest"] = run
        if run["status"] != "completed" or run["returncode"] not in (0, 1):
            report["status"] = "execution_failed"
        else:
            parsed = load_junit_report(directory / "run.xml")[2]
            if (not parsed["records"] or (run["returncode"] == 0) != (parsed["summary"]["failed"] == 0)
                    or run["executed_manifest"] != source["manifest"]):
                raise ValueError("测试退出码、XML 或执行源码不一致。")
            report.update(status="tests_failed" if parsed["summary"]["failed"] else "tests_passed", summary=parsed["summary"])
        if (snapshot_sources(source["root"])["manifest"] != source["manifest"]
                or load_test_config(test_config, source["manifest"])[2] != config_source):
            raise ValueError("测试期间原源码或配置变化。")
    except (Exception, KeyboardInterrupt) as error:
        report.update(status="cancelled" if isinstance(error, KeyboardInterrupt) else "partial", error_type=type(error).__name__)
    write_json_atomic(directory / "report.json", report)
    return report
