"""导出运行包，在新 venv 安装并验收；原环境和已有输出均不覆盖。"""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import zipfile


CHECKS = {"fresh_environment_empty", "pip_dependencies_consistent", "imports_from_extracted_project_and_new_venv",
          "doctor_ready_without_credentials", "agent_help_available", "offline_files_plan_session_restore",
          "sigterm_single_run_rollback", "source_package_unchanged"}
INFO = """import importlib.util,json,site,sys
from pathlib import Path
print(json.dumps({'prefix':sys.prefix,'base_prefix':sys.base_prefix,'executable':sys.executable,
 'user_site_enabled':site.ENABLE_USER_SITE,'libraries_present':{name:importlib.util.find_spec(name) is not None for name in ('openai','anthropic','dotenv')}}))
"""
PROVENANCE = """import importlib.metadata as md,json,site,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import personal_agent,openai,anthropic,dotenv
print(json.dumps({'prefix':sys.prefix,'base_prefix':sys.base_prefix,'executable':sys.executable,
 'user_site_enabled':site.ENABLE_USER_SITE,
 'modules':{m.__name__:str(Path(m.__file__).resolve()) for m in (personal_agent,openai,anthropic,dotenv)},
 'versions':{name:md.version(name) for name in ('openai','anthropic','python-dotenv')},
 'installed':{d.metadata['Name']:d.version for d in md.distributions()}}))
"""
PROBE = "import runpy,sys; sys.path.insert(0,sys.argv[1]); sys.argv=['container_probe',*sys.argv[2:]]; runpy.run_module('personal_agent.container_probe',run_name='__main__')"


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def clean_environment():
    """只修改子进程副本；隔离 Python/pip 注入配置与模型凭据。"""
    result = {key: value for key, value in os.environ.items() if not key.upper().startswith(
        ("PYTHON", "PIP", "PERSONAL_AGENT_", "ANTHROPIC_", "OPENAI_")) and key.upper() not in {"DASHSCOPE_API_KEY", "VIRTUAL_ENV"}}
    result.update(PYTHONUTF8="1", PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1", PIP_CONFIG_FILE=os.devnull,
                  PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_INPUT="1")
    return result


class ReleaseProcess:
    real = True

    def __init__(self, directory, environment):
        self.directory, self.environment = directory, environment
        self.secrets = [os.getenv(key, "") for key in ("DASHSCOPE_API_KEY", "ANTHROPIC_API_KEY") if len(os.getenv(key, "")) > 8]

    def _redact(self, value):
        for secret in self.secrets:
            value = value.replace(secret, "<redacted>")
        return re.sub(r"(https?://)[^/\s]+@", r"\1<redacted>@", value)

    def run(self, stage, command, *, cwd, expected=0, timeout=60):
        record = {"stage": stage, "command": command, "cwd": str(cwd), "expected_returncode": expected}
        try:
            process = subprocess.run(command, cwd=cwd, env=self.environment, stdin=subprocess.DEVNULL,
                                     capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            record.update(status="failed", error_type="TimeoutExpired")
            _write(self.directory / f"{stage}.json", record)
            text = (error.stdout or b"").decode("utf-8", errors="replace") + "\n" + (error.stderr or b"").decode("utf-8", errors="replace")
            (self.directory / f"{stage}.log").write_text(self._redact(text), encoding="utf-8")
            raise RuntimeError(f"release stage timed out: {stage}") from None
        stdout = self._redact(process.stdout.decode("utf-8", errors="replace"))
        stderr = self._redact(process.stderr.decode("utf-8", errors="replace"))
        (self.directory / f"{stage}.log").write_text(stdout + "\n" + stderr, encoding="utf-8")
        record.update(returncode=process.returncode, status="passed" if process.returncode == expected else "failed")
        _write(self.directory / f"{stage}.json", record)
        if process.returncode != expected:
            raise RuntimeError(f"release stage failed: {stage}; see {stage}.log")
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _inside(path, root):
    path = Path(path).resolve()
    return path == root or root in path.parents


def _check_source(directory):
    manifest = json.loads((directory / "package/manifest.json").read_text(encoding="utf-8"))
    for name, entry in manifest["entries"].items():
        path = directory / "project" / name
        if path.resolve() != path or _sha(path) != entry["sha256"]:
            raise ValueError("解压源码与运行包指纹不一致。")


def _check_identity(directory, info, *, before=False):
    project = directory / "project"
    environment = project / ".venv"
    if (Path(info["prefix"]).resolve() != environment or Path(info["base_prefix"]).resolve() == environment
            or not _inside(info["executable"], environment) or info["user_site_enabled"] is not False):
        raise ValueError("验收进程没有使用新建的独立虚拟环境。")
    config = (environment / "pyvenv.cfg").read_text(encoding="utf-8").lower()
    if "include-system-site-packages = false" not in config:
        raise ValueError("新环境不得继承全局 site-packages。")
    if before:
        if set(info["libraries_present"]) != {"openai", "anthropic", "dotenv"} or any(info["libraries_present"].values()):
            raise ValueError("安装前不应已存在 SDK 依赖。")
        return
    if Path(info["modules"]["personal_agent"]).resolve() != project / "personal_agent/__init__.py":
        raise ValueError("应用并非来自本次解压副本。")
    if any(not _inside(info["modules"][name], environment) for name in ("openai", "anthropic", "dotenv")):
        raise ValueError("SDK 并非来自新虚拟环境。")
    expected = dict(line.split("==") for line in (project / "requirements.txt").read_text().splitlines() if line.strip())
    if info["versions"] != expected:
        raise ValueError("SDK 版本与直接锁定依赖不一致。")


def run_release_check(output_dir, *, _runner_factory=None):
    if not __debug__:
        raise ValueError("交付验收需要完整检查，不支持 -O 或 PYTHONOPTIMIZE。")
    directory = Path(output_dir).absolute()
    if directory.resolve() != directory or directory.exists():
        raise ValueError("交付验收须使用新的、无链接重定向的输出目录。")
    directory.mkdir(parents=True, exist_ok=False)
    runner = (_runner_factory or ReleaseProcess)(directory, clean_environment())
    report = {"version": 1, "kind": "clean_install_release", "status": "running", "directory": str(directory), "checks": {},
              "clean_install_verified": False, "new_remote_model_calls": 0, "container_verified": False,
              "validation": "real new venv and pip install" if runner.real else "injected process fixture; not a clean installation",
              "boundary": "same-host Python base, separate packages/source/cwd; not a new OS, bundled interpreter, container or native provider signature acceptance"}
    def save():
        _write(directory / "report.json", report)
    def record(name, command, **options):
        report["active_stage"] = name; save()
        return runner.run(name, command, cwd=caller, **options)
    try:
        from .delivery_bundle import check_package, create_package
        package = create_package(directory / "package")
        check_package(package["manifest"])
        project, caller = directory / "project", directory / "external caller 中文"
        project.mkdir(); caller.mkdir()
        with zipfile.ZipFile(package["archive"]) as archive:
            archive.extractall(project)  # 仅解压刚核验过的自产包。
        _check_source(directory)
        record("create-venv", [sys.executable, "-X", "utf8", "-I", "-m", "venv", "--copies", str(project / ".venv")], timeout=120)
        python = str(project / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python"))
        before = json.loads(record("before-install", [python, "-X", "utf8", "-I", "-B", "-c", INFO]).stdout)
        _check_identity(directory, before, before=True); _write(directory / "before-install-info.json", before)
        report["checks"]["fresh_environment_empty"] = True
        record("install", [python, "-X", "utf8", "-m", "pip", "install", "--no-input", "--disable-pip-version-check", "--only-binary=:all:",
                           "--index-url", "https://pypi.org/simple", "--timeout", "15", "--retries", "1", "--report", str(directory / "install-report.json"),
                           "-r", str(project / "requirements.txt")], timeout=300)
        record("pip-check", [python, "-X", "utf8", "-m", "pip", "check"])
        report["checks"]["pip_dependencies_consistent"] = True
        frozen = record("freeze", [python, "-X", "utf8", "-m", "pip", "freeze", "--all"]).stdout
        (directory / "freeze.txt").write_text(frozen, encoding="utf-8")
        info = json.loads(record("provenance", [python, "-X", "utf8", "-I", "-B", "-c", PROVENANCE, str(project)]).stdout)
        _check_identity(directory, info); _write(directory / "environment.json", info)
        report["checks"]["imports_from_extracted_project_and_new_venv"] = True
        doctor = json.loads(record("doctor", [python, "-X", "utf8", "-E", "-s", "-B", str(project / "run_demo.py"), "doctor"]).stdout)
        assert doctor["status"] == "ready" and doctor["project_root"] == str(project) and not any(doctor["credential_presence"].values())
        report["checks"]["doctor_ready_without_credentials"] = True
        help_text = record("agent-help", [python, "-X", "utf8", "-E", "-s", "-B", str(project / "run_agent.py"), "--help"]).stdout
        assert "--prompt" in help_text and "--anthropic-provider" in help_text
        report["checks"]["agent_help_available"] = True
        state, workspace = directory / "state", directory / "workspace"
        command = [python, "-X", "utf8", "-I", "-B", "-c", PROBE, str(project)]
        args = ["--state-dir", str(state), "--workspace-dir", str(workspace)]
        smoke = json.loads(record("offline-smoke", [*command, "smoke", *args]).stdout.strip().splitlines()[-1])
        assert smoke["status"] == "passed" and smoke["container_constraints_verified"] is False
        checked = json.loads(record("session-restore", [*command, "verify", *args]).stdout.strip().splitlines()[-1])
        assert checked["status"] == "passed" and checked["fresh_process_session_restore_no_replay"] is True
        report["checks"]["offline_files_plan_session_restore"] = True
        record("sigterm", [*command, "signal", *args, "--self-signal"], expected=143)
        checked = json.loads(record("after-stop", [*command, "verify", *args, "--require-signal"]).stdout.strip().splitlines()[-1])
        assert checked["status"] == "passed" and checked["signal_rollback_checked"] is True
        report["checks"]["sigterm_single_run_rollback"] = True
        if os.name == "nt":
            launched = json.loads(record("cmd-launcher", f'cmd.exe /d /s /c ""{project / "demo.cmd"}" doctor"').stdout)
            assert launched["status"] == "ready" and launched["project_root"] == str(project)
            report["checks"]["windows_cmd_launcher"] = True
        check_package(package["manifest"]); _check_source(directory)
        report["checks"]["source_package_unchanged"] = True
        files = [directory / name for name in ("package/manifest.json", "package/personal-agent.zip", "project/.venv/pyvenv.cfg",
                 "before-install-info.json", "environment.json", "install-report.json", "freeze.txt")]
        files += [path for folder in (state, workspace) for path in folder.rglob("*") if path.is_file()]
        files += sorted(directory.glob("*.log"))
        report["evidence_files"] = {path.relative_to(directory).as_posix(): _sha(path) for path in files}
        report.update(status="passed", clean_install_verified=runner.real)
    except KeyboardInterrupt:
        report["status"] = "cancelled"
    except (OSError, ValueError, RuntimeError, AssertionError, ImportError, subprocess.SubprocessError) as error:
        report.update(status="failed", error_type=type(error).__name__)
    save()
    return report


def check_release_evidence(report):
    """只读核对保留的源码、安装来源与演示产物，不执行导出包中的代码。"""
    if not __debug__:
        raise ValueError("交付证据检查不支持优化模式。")
    from .delivery_bundle import check_package
    from .delivery_demo import check_demo
    from .session import validate_agent_messages
    path = Path(report).absolute()
    if path.resolve() != path or path.name != "report.json":
        raise ValueError("须指定原目录内的 report.json。")
    directory = path.parent
    value = json.loads(path.read_text(encoding="utf-8"))
    if (value.get("version") != 1 or value.get("kind") != "clean_install_release" or value.get("status") != "passed"
            or value.get("clean_install_verified") is not True or value.get("directory") != str(directory)
            or not CHECKS <= set(value.get("checks", {})) or not all(value["checks"].values())):
        raise ValueError("不是实际通过的全新安装验收报告。")
    hashes = value.get("evidence_files", {})
    required = {"package/manifest.json", "package/personal-agent.zip", "project/.venv/pyvenv.cfg", "before-install-info.json", "environment.json", "install-report.json",
                "freeze.txt", "state/signal-result.json", "state/demo/report.json", "workspace/.personal/sessions/container-signal.json"}
    if not isinstance(hashes, dict) or not required <= set(hashes) or len(hashes) > 1000:
        raise ValueError("交付证据清单不完整。")
    for name, expected in hashes.items():
        relative = PurePosixPath(name)
        target = directory / name
        if (relative.as_posix() != name or relative.is_absolute() or any(p in {".", ".."} for p in relative.parts)
                or "\\" in name or ":" in name or target.resolve() != target or not _inside(target, directory) or _sha(target) != expected):
            raise ValueError("交付证据路径或文件指纹变化。")
    check_package(directory / "package/manifest.json"); _check_source(directory)
    _check_identity(directory, json.loads((directory / "before-install-info.json").read_text(encoding="utf-8")), before=True)
    _check_identity(directory, json.loads((directory / "environment.json").read_text(encoding="utf-8")))
    check_demo(directory / "state/demo/report.json")
    saved = json.loads((directory / "workspace/.personal/sessions/container-acceptance.json").read_text(encoding="utf-8"))
    if validate_agent_messages(saved["messages"]) != json.loads((directory / "state/demo/transcript.json").read_text(encoding="utf-8")):
        raise ValueError("交付会话与实际演示轨迹不一致。")
    stopped = json.loads((directory / "state/signal-result.json").read_text(encoding="utf-8"))
    if not (stopped["status"] == "passed" and stopped["exit_code"] == 143 and stopped["history_rolled_back"] and stopped["signal_handler_restored"]):
        raise ValueError("停止信号验收记录不完整。")
    signal_session = json.loads((directory / "workspace/.personal/sessions/container-signal.json").read_text(encoding="utf-8"))
    if validate_agent_messages(signal_session["messages"]) != [{"role": "user", "content": "previous completed question"}, {"role": "assistant", "content": "previous completed answer"}]:
        raise ValueError("停止后未保留预期完整会话。")
    return {"status": "passed", "report": str(path), "files": len(hashes), "new_remote_model_calls": 0,
            "validation": "read-only file hashes, import origins and actual artifacts; no Python install, tool replay or new model request"}
