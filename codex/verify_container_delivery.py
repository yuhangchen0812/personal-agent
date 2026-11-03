"""本地真实 CLI/文件/信号与注入引擎事务检查；从不冒充真实 Docker 验收。"""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from personal_agent.container_check import run_container_acceptance
from personal_agent.cancellation import termination_requested, termination_scope


class FixtureEngine:
    real = False

    def __init__(self, *, failure=None):
        self.commands = []
        self.failure = failure

    def run(self, arguments, *, timeout=60, check=True):
        self.commands.append(arguments)
        code, output = 0, ""
        if arguments[:1] == ["info"]:
            code = 1 if self.failure == "engine" else 0
            output = "windows\n" if self.failure == "windows" else "linux\n"
        elif arguments[:2] == ["image", "inspect"]:
            output = json.dumps({"User": "10001:10001", "WorkingDir": "/app", "StopSignal": "SIGTERM",
                                 "Entrypoint": ["python", "-m", "personal_agent", "--deny-tool", "run_shell"], "Cmd": ["--help"]})
        elif arguments[0] == "run":
            if "--entrypoint" not in arguments:
                output = "fixture help --prompt"
            elif "signal" in arguments:
                output = "fixture container id"
            elif "smoke" in arguments:
                if self.failure == "smoke":
                    raise RuntimeError("injected container smoke failure")
                output = json.dumps({"status": "passed", "container_constraints_verified": True})
            else:
                output = json.dumps({"status": "passed", "fresh_process_session_restore_no_replay": True, "signal_rollback_checked": True})
        elif arguments[0] == "wait":
            output = "137" if self.failure == "signal" else "143"
        elif arguments[0] == "cp":
            destination = Path(arguments[-1]); destination.mkdir()
            if arguments[-2].endswith("/container-acceptance") and destination.name == "artifacts":
                (destination / "probe.json").write_text(json.dumps({"container_constraints_verified": True}), encoding="utf-8")
                (destination / "signal-result.json").write_text(json.dumps({"status": "passed", "exit_code": 143, "signal_handler_restored": True}), encoding="utf-8")
                summary = destination / "demo/workspace/playground/summary.json"
                summary.parent.mkdir(parents=True)
                summary.write_text('{"total":10,"count":3}', encoding="utf-8")
        elif arguments[:2] == ["volume", "rm"] and self.failure == "cleanup":
            code = 1
        return subprocess.CompletedProcess(arguments, code, output, "")


def verify_workflow():
    with tempfile.TemporaryDirectory(prefix="container-delivery-中文 空格-") as temporary:
        root = Path(temporary).resolve()
        optimized = subprocess.run([sys.executable, "-O", "-m", "personal_agent.delivery", "container-check", "--output-dir", str(root / "optimized")],
                                   cwd=ROOT, capture_output=True, timeout=20)
        assert optimized.returncode == 2 and not (root / "optimized").exists()
        environment = dict(os.environ)
        for name in ("DASHSCOPE_API_KEY", "ANTHROPIC_API_KEY"):
            environment.pop(name, None)
        def probe(action, state, workspace, *extra, expected=0):
            command = [sys.executable, "-X", "utf8", "-B", "-m", "personal_agent.container_probe", action,
                       "--state-dir", str(state), "--workspace-dir", str(workspace), *extra]
            process = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, timeout=60)
            assert process.returncode == expected, process.stderr.decode("utf-8", errors="replace")
            if action != "signal":
                return json.loads(process.stdout.strip().splitlines()[-1])
            report = json.loads((state / "signal-result.json").read_text(encoding="utf-8"))
            assert report["status"] == "passed" and report["history_rolled_back"] and report["model_context_closed"] and report["signal_handler_restored"]
            assert report["transport"] == "in-process signal.raise_signal"
            if "--repl" not in extra:
                single = json.loads(process.stdout.decode("utf-8").split('\n{"status": "passed"')[0])
                assert single["status"] == "cancelled" and single["exit_code"] == 143
            return report
        state, workspace = root / "state", root / "workspace"
        result = probe("smoke", state, workspace)
        assert result["status"] == "passed" and not result["container_constraints_verified"]
        assert probe("smoke", root / "strict-state", root / "strict-work", "--require-container", expected=1)["status"] == "failed"
        assert probe("verify", state, workspace)["fresh_process_session_restore_no_replay"]
        for repl in (False, True):
            probe("signal", root / ("repl-state" if repl else "signal-state"), root / ("repl-work" if repl else "signal-work"),
                  "--self-signal", *(["--repl"] if repl else []), expected=143)
        # 作用域退出要恢复原处理器；普通 Ctrl+C 不会被当作整进程 SIGTERM。
        previous = signal.getsignal(signal.SIGTERM)
        with termination_scope():
            try:
                raise KeyboardInterrupt
            except KeyboardInterrupt:
                assert not termination_requested()
        assert signal.getsignal(signal.SIGTERM) == previous
        with patch("personal_agent.container_check.shutil.which", return_value=None):
            unavailable = run_container_acceptance(root / "not-ready")
        assert unavailable["status"] == "not_ready" and not unavailable["container_build_or_run_verified"]
        for failure in ("engine", "windows"):
            engine = FixtureEngine(failure=failure)
            report = run_container_acceptance(root / failure, _engine=engine)
            assert report["status"] == "not_ready" and not any(cmd[0] == "build" for cmd in engine.commands)
        for failure in (None, "smoke", "signal", "cleanup"):
            engine = FixtureEngine(failure=failure)
            report = run_container_acceptance(root / (failure or "fixture-success"), _engine=engine)
            assert report["status"] == ("passed" if failure is None else "failed")
            assert not report["container_profile_fully_accepted"] and not report["container_build_or_run_verified"]
            volumes = [cmd[-1] for cmd in engine.commands if cmd[:2] == ["volume", "create"]]
            removed = [cmd[-1] for cmd in engine.commands if cmd[:2] == ["volume", "rm"]]
            assert sorted(removed) == sorted(volumes) and len(volumes) == 2
            assert all(name.startswith("pa-check-") for name in removed)
            assert not any("prune" in cmd for cmd in engine.commands)
            if failure == "smoke":
                assert not any(cmd[0] == "stop" for cmd in engine.commands)
    return {"status": "passed", "validation": "actual Windows/host child processes, fixed models/files/Plan/Session; in-process SIGTERM; injected Docker lifecycle only",
            "actual_single_run_and_repl_stop_143_rollback_cleanup_and_handler_restore": True,
            "fresh_process_demo_session_restore_and_host_not_promoted_to_container": True, "failed_smoke_sigkill_and_cleanup_never_promoted": True,
            "only_unique_owned_engine_resources_removed": True, "new_remote_model_calls": 0,
            "real_container_build_run_or_external_sigterm_verified": False}


if __name__ == "__main__":
    print(json.dumps(verify_workflow(), ensure_ascii=False))
