"""构建并验收本次唯一镜像/卷；没有 Docker 时明确退出，不以模拟替代真实容器。"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from uuid import uuid4
import zipfile

from .delivery import ROOT


STATE = "/app/.personal/container-acceptance"
WORKSPACE = "/app/playground/container-acceptance"


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class Engine:
    real = True

    def __init__(self, executable, directory):
        self.executable, self.directory, self.count = executable, directory, 0

    def run(self, arguments, *, timeout=60, check=True):
        self.count += 1
        name = f"command-{self.count:03d}"
        environment = dict(os.environ)
        for key in ("DASHSCOPE_API_KEY", "ANTHROPIC_API_KEY"):
            environment.pop(key, None)
        command = [self.executable, *arguments]
        try:
            process = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, env=environment, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            (self.directory / f"{name}.log").write_bytes((error.stdout or b"") + b"\n" + (error.stderr or b""))
            _write(self.directory / f"{name}.json", {"command": command, "status": "timeout", "timeout_seconds": timeout})
            raise RuntimeError("container command timed out") from None
        (self.directory / f"{name}.log").write_bytes(process.stdout + b"\n" + process.stderr)
        _write(self.directory / f"{name}.json", {"command": command, "returncode": process.returncode})
        result = subprocess.CompletedProcess(command, process.returncode, process.stdout.decode("utf-8", errors="replace"), process.stderr.decode("utf-8", errors="replace"))
        if check and result.returncode:
            raise RuntimeError(f"container command failed; see {name}.log")
        return result


def run_container_acceptance(output_dir, *, _engine=None):
    if not __debug__:
        raise ValueError("容器验收依赖完整检查，不能使用 -O 或 PYTHONOPTIMIZE。")
    directory = Path(output_dir).absolute()
    if directory.resolve() != directory or directory.exists():
        raise ValueError("容器验收输出须为新的、无链接重定向目录。")
    directory.mkdir(parents=True, exist_ok=False)
    report = {"version": 1, "kind": "container_acceptance", "status": "running", "directory": str(directory),
              "checks": {}, "cleanup": [], "new_remote_model_calls": 0, "container_profile_fully_accepted": False,
              "boundary": "explicit Linux file-task profile; fixed model, actual CLI/tools/session; native provider signatures not covered"}
    executable = shutil.which("docker") if _engine is None else "fixture"
    if executable is None:
        report.update(status="not_ready", reason="docker_client_missing", container_build_or_run_verified=False)
        _write(directory / "report.json", report)
        return report
    engine = _engine or Engine(executable, directory)
    report["validation"] = "real Docker engine" if engine.real else "injected engine fixture; not a real container"
    identifier = uuid4().hex
    image = f"personal-agent-acceptance:{identifier}"
    volumes = [f"pa-check-{identifier}-state", f"pa-check-{identifier}-workspace"]
    containers, created_volumes = [], []
    image_attempted = False
    report["owned_resources"] = {"image": image, "volumes": volumes, "containers": containers}
    def persist():
        _write(directory / "report.json", report)
    def invoke(action, *extra, detach=False):
        name = f"pa-check-{identifier}-{action}"
        containers.append(name); persist()
        options = ["run", "--name", name, "--label", f"personal-agent-check={identifier}",
                   "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                   "--tmpfs", "/tmp:rw,nosuid,nodev,size=16m", "--mount", f"type=volume,source={volumes[0]},target=/app/.personal",
                   "--mount", f"type=volume,source={volumes[1]},target=/app/playground"]
        if detach:
            options += ["--detach"]
        if action == "help":
            return name, engine.run([*options, image])
        command = "verify" if action in {"restore", "final"} else action
        args = ["--entrypoint", "python", image, "-m", "personal_agent.container_probe", command,
                "--state-dir", STATE, "--workspace-dir", WORKSPACE, *extra]
        return name, engine.run([*options, *args])
    try:
        # 不探测任意服务地址；先确认当前 Docker Engine 确实可用且为 Linux。
        info = engine.run(["info", "--format", "{{.OSType}}"], timeout=8, check=False)
        if info.returncode:
            report.update(status="not_ready", reason="docker_engine_unreachable")
            return report
        if info.stdout.strip() != "linux":
            report.update(status="not_ready", reason="linux_engine_required")
            return report
        from .delivery_bundle import create_package, check_package
        package = create_package(directory / "package")
        check_package(package["manifest"])
        report["package_manifest"] = str(Path(package["manifest"]).relative_to(directory))
        context = directory / "context"
        context.mkdir()
        with zipfile.ZipFile(package["archive"]) as archive:
            archive.extractall(context)  # 已逐项验证本次自产允许清单和指纹。
        image_attempted = True; persist()
        engine.run(["build", "--label", f"personal-agent-check={identifier}", "--tag", image, str(context)], timeout=600)
        configuration = json.loads(engine.run(["image", "inspect", "--format", "{{json .Config}}", image]).stdout)
        expected_entry = ["python", "-m", "personal_agent", "--deny-tool", "run_shell"]
        assert configuration["User"] == "10001:10001" and configuration["WorkingDir"] == "/app"
        assert configuration["Entrypoint"] == expected_entry and configuration["Cmd"] == ["--help"]
        assert configuration.get("StopSignal") == "SIGTERM"
        report["checks"]["image_nonroot_entrypoint_and_stop_signal"] = True
        for volume in volumes:
            created_volumes.append(volume); persist()
            engine.run(["volume", "create", "--label", f"personal-agent-check={identifier}", volume])
        _, help_result = invoke("help")
        assert "--prompt" in help_result.stdout
        report["checks"]["actual_default_entrypoint_help"] = True
        _, smoke_result = invoke("smoke", "--require-container")
        smoke = json.loads(smoke_result.stdout.strip().splitlines()[-1])
        assert smoke["status"] == "passed" and smoke["container_constraints_verified"] is True
        report["checks"]["nonroot_volumes_readonly_network_off_and_actual_demo"] = True
        _, restore_result = invoke("restore")
        assert json.loads(restore_result.stdout.strip().splitlines()[-1])["fresh_process_session_restore_no_replay"] is True
        report["checks"]["fresh_container_persistence_without_tool_replay"] = True
        signal_name, _ = invoke("signal", detach=True)
        ready = False
        ready_deadline = time.monotonic() + 15
        for _ in range(50):
            if time.monotonic() >= ready_deadline:
                break
            process = engine.run(["exec", signal_name, "python", "-c", f"from pathlib import Path; import sys; sys.exit(0 if Path('{STATE}/ready.json').exists() else 1)"], timeout=3, check=False)
            if process.returncode == 0:
                ready = True; break
            time.sleep(0.1)
        assert ready, "CLI did not reach active model request"
        engine.run(["stop", "--timeout", "5", signal_name], timeout=15)
        assert engine.run(["wait", signal_name]).stdout.strip() == "143"
        report["checks"]["external_sigterm_reaches_active_cli_and_exits_143"] = True
        final_name, final_result = invoke("final", "--require-signal")
        final = json.loads(final_result.stdout.strip().splitlines()[-1])
        assert final["status"] == "passed" and final["signal_rollback_checked"] is True
        report["checks"]["fresh_container_verifies_signal_history_rollback"] = True
        engine.run(["cp", f"{final_name}:{STATE}", str(directory / "artifacts")])
        engine.run(["cp", f"{final_name}:{WORKSPACE}", str(directory / "workspace")])
        probe = json.loads((directory / "artifacts/probe.json").read_text(encoding="utf-8"))
        stopped = json.loads((directory / "artifacts/signal-result.json").read_text(encoding="utf-8"))
        assert probe["container_constraints_verified"] is True
        assert stopped["status"] == "passed" and stopped["exit_code"] == 143 and stopped["signal_handler_restored"] is True
        assert json.loads((directory / "artifacts/demo/workspace/playground/summary.json").read_text(encoding="utf-8")) == {"total": 10, "count": 3}
        report["checks"]["copied_artifacts_rechecked_on_host"] = True
        report["status"] = "passed"
    except KeyboardInterrupt:
        report["status"] = "cancelled"
    except (OSError, ValueError, RuntimeError, AssertionError, ImportError, zipfile.BadZipFile) as error:
        report.update(status="failed", error_type=type(error).__name__)
    finally:
        for name in containers:
            try:
                engine.run(["logs", name], check=False)
            except (OSError, RuntimeError):
                pass  # 日志读取失败不阻止回收本次资源；实际清理结果单独记录。
        resources = [(["rm", "--force", name], "container", name) for name in reversed(containers)]
        resources += [(["volume", "rm", name], "volume", name) for name in reversed(created_volumes)]
        if image_attempted:
            resources.append((["image", "rm", image], "image", image))
        for command, kind, name in resources:
            try:
                process = engine.run(command, check=False)
                report["cleanup"].append({"kind": kind, "name": name, "returncode": process.returncode})
            except (OSError, RuntimeError):
                report["cleanup"].append({"kind": kind, "name": name, "returncode": None})
        if any(item["returncode"] != 0 for item in report["cleanup"]) and report["status"] == "passed":
            report["status"] = "failed"
            report["error_type"] = "ResourceCleanupFailed"
        report["container_build_or_run_verified"] = engine.real and bool(report["checks"].get("nonroot_volumes_readonly_network_off_and_actual_demo"))
        report["container_profile_fully_accepted"] = engine.real and report["status"] == "passed"
        persist()
    return report
