"""已知 pytest 演示的本地执行辅助；调用方负责审阅与限定源码，不是沙箱。"""

from importlib.metadata import version
import os
from pathlib import Path
import subprocess
import sys

from .source_snapshot import digest


DEMO = Path(__file__).resolve().parent.parent / "examples/pytest-ci-demo"


def code_hashes(directory):
    return {path.name: digest(path.read_bytes()) for path in Path(directory).iterdir()
        if path.is_file() and path.suffix in {".py", ".ini"}}


def execute_pytest(directory, xml):
    xml.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-X", "utf8", "-B", "-m", "pytest", "-q", "-c", "pytest.ini", "-p", "no:cacheprovider",
        "--tb=short", "--color=no", f"--junitxml={xml}"]
    environment = os.environ.copy()
    environment.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTEST_ADDOPTS="", PYTHONIOENCODING="utf-8")
    environment.pop("PYTEST_PLUGINS", None)
    process = subprocess.run(command, cwd=directory, env=environment, capture_output=True, timeout=30)
    log = xml.with_suffix(".log")
    log.write_text(process.stdout.decode("utf-8", errors="replace") + process.stderr.decode("utf-8", errors="replace"), encoding="utf-8")
    if not xml.is_file():
        raise ValueError(f"pytest 未生成 XML，请查看 {log}")
    return {"command": command, "directory": str(directory), "xml": str(xml), "log": str(log),
        "returncode": process.returncode, "pytest_version": version("pytest"), "executed_source": code_hashes(directory)}
