"""显式离线验收探针；固定模型与受限演示审批，不调用外部模型。"""

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import platform
import signal
import time
from unittest.mock import patch

from .delivery import doctor
from .delivery_demo import check_demo, run_demo
from .file_runtime import write_json_atomic
from .session import Session
from .source_snapshot import digest
from .tool_registry import execute_tool


PREVIOUS = [{"role": "user", "content": "previous completed question"},
            {"role": "assistant", "content": "previous completed answer"}]


def environment_facts():
    linux = platform.system() == "Linux"
    facts = {"linux": linux, "uid": os.getuid() if hasattr(os, "getuid") else None,
             "gid": os.getgid() if hasattr(os, "getgid") else None, "pid": os.getpid(),
             "root_read_only": False, "network_only_loopback": False, "capabilities_dropped": False,
             "no_new_privileges": False}
    if linux:
        facts["root_read_only"] = any(line.split()[4] == "/" and "ro" in line.split()[5].split(",")
                                      for line in Path("/proc/self/mountinfo").read_text().splitlines())
        facts["network_only_loopback"] = sorted(p.name for p in Path("/sys/class/net").iterdir()) == ["lo"]
        status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
        facts["capabilities_dropped"] = int(status.get("CapEff", "1").strip(), 16) == 0
        facts["no_new_privileges"] = status.get("NoNewPrivs", "0").strip() == "1"
    return facts


def _container_ok(facts):
    return (facts["linux"] and facts["uid"] == facts["gid"] == 10001 and facts["root_read_only"]
            and facts["network_only_loopback"] and facts["capabilities_dropped"] and facts["no_new_privileges"])


def smoke(state_dir, workspace_dir, *, require_container=False):
    state, workspace = Path(state_dir).resolve(), Path(workspace_dir).resolve()
    state.mkdir(parents=True, exist_ok=False); workspace.mkdir(parents=True, exist_ok=False)
    demo = run_demo(state / "demo")
    history = json.loads((state / "demo/transcript.json").read_text(encoding="utf-8"))
    with patch("personal_agent.session.PROJECT_ROOT", workspace):
        session = Session("container-acceptance", with_tools=True)
        session.create(); session.save(history)
        session_hash = digest(session.path.read_bytes())
    denied = execute_tool("run_shell", {"command": "Write-Output UNEXPECTED"}, mode="dontAsk", deny=["run_shell"])
    facts = environment_facts()
    checks = {"offline_demo_passed": demo["status"] == "passed", "shell_explicitly_denied": "权限拒绝" in denied,
              "runtime_dependencies_ready": doctor()["status"] == "ready",
              "no_model_credentials": not any(os.getenv(key) for key in ("DASHSCOPE_API_KEY", "ANTHROPIC_API_KEY"))}
    if require_container:
        checks["container_constraints"] = _container_ok(facts)
        checks["volume_file_owner"] = session.path.stat().st_uid == (state / "demo/report.json").stat().st_uid == 10001
    report = {"version": 1, "kind": "container_acceptance_probe", "status": "passed" if all(checks.values()) else "failed",
              "state_dir": str(state), "workspace_dir": str(workspace), "checks": checks, "facts": facts,
              "session_sha256": session_hash, "require_container": require_container,
              "container_constraints_verified": require_container and _container_ok(facts),
              "new_remote_model_calls": 0, "validation": "fixed model; actual file/Plan/Session; container constraints only when explicitly required"}
    write_json_atomic(state / "probe.json", report)
    return report


def verify(state_dir, workspace_dir, *, require_signal=False):
    state, workspace = Path(state_dir).resolve(), Path(workspace_dir).resolve()
    report = json.loads((state / "probe.json").read_text(encoding="utf-8"))
    assert report["status"] == "passed" and report["workspace_dir"] == str(workspace)
    checked = check_demo(state / "demo/report.json")
    with patch("personal_agent.session.PROJECT_ROOT", workspace), patch("personal_agent.tool_registry.execute_tool", side_effect=AssertionError("no replay")):
        session = Session("container-acceptance", with_tools=True)
        history = session.load()
        assert digest(session.path.read_bytes()) == report["session_sha256"]
        assert history == json.loads((state / "demo/transcript.json").read_text(encoding="utf-8"))
        if require_signal:
            result = json.loads((state / "signal-result.json").read_text(encoding="utf-8"))
            assert result["status"] == "passed" and result["exit_code"] == 143
            assert Session("container-signal", with_tools=True).load() == PREVIOUS
    facts = environment_facts()
    if report["require_container"]:
        assert _container_ok(facts)
    return {"status": "passed", "demo": checked["status"], "fresh_process_session_restore_no_replay": True,
            "signal_rollback_checked": require_signal, "container_constraints_verified": report["require_container"] and _container_ok(facts),
            "new_remote_model_calls": 0}


def signal_task(state_dir, workspace_dir, *, self_signal=False, repl=False):
    """在实际 CLI 模型调用内等待停止；self_signal 仅供本机 C-runtime 信号测试。"""
    from .main import main
    state, workspace = Path(state_dir).resolve(), Path(workspace_dir).resolve()
    state.mkdir(parents=True, exist_ok=True); workspace.mkdir(parents=True, exist_ok=True)
    previous_handler = signal.getsignal(signal.SIGTERM)
    model_closed = []
    def model(messages, *args, **kwargs):
        assert messages[-1]["role"] == "user" and messages[-1]["content"] == "active request"
        write_json_atomic(state / "ready.json", {"status": "ready", "pid": os.getpid(), "in_actual_cli_request": True})
        try:
            if self_signal:
                signal.raise_signal(signal.SIGTERM)
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                time.sleep(0.05)
            raise RuntimeError("signal probe timed out")
        finally:
            model_closed.append(True)
    code = 0
    with ExitStack() as stack:
        stack.enter_context(patch("personal_agent.session.PROJECT_ROOT", workspace))
        stack.enter_context(patch("personal_agent.config.ENV_FILE", state / "absent.env"))
        stack.enter_context(patch("personal_agent.main.get_model_message", side_effect=model))
        stack.enter_context(patch("personal_agent.main.discover_skills", return_value=([], [])))
        stack.enter_context(patch("personal_agent.main.discover_agents", return_value=([], [])))
        if repl:
            stack.enter_context(patch("builtins.input", side_effect=["active request", AssertionError("SIGTERM must exit REPL")] ))
        session = Session("container-signal", with_tools=True)
        session.create(); session.save(PREVIOUS)
        before = session.load()
        args = ["--protocol", "openai", "--model", "offline-signal-fixture", "--api-base", "https://example.invalid/v1",
                "--thinking", "disabled", "--resume", "container-signal", "--deny-tool", "run_shell"]
        if not repl:
            args += ["--prompt", "active request", "--output-format", "json"]
        try:
            main(args)
        except SystemExit as error:
            code = error.code
        result = {"status": "passed" if code == 143 and session.load() == before and model_closed == [True] else "failed",
                  "exit_code": code, "history_rolled_back": session.load() == PREVIOUS, "model_context_closed": model_closed == [True],
                  "signal_handler_restored": signal.getsignal(signal.SIGTERM) == previous_handler,
                  "transport": "in-process signal.raise_signal" if self_signal else "external SIGTERM",
                  "repl": repl, "new_remote_model_calls": 0}
        if not result["signal_handler_restored"]:
            result["status"] = "failed"
        write_json_atomic(state / "signal-result.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("smoke", "verify", "signal"))
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--workspace-dir", type=Path, required=True)
    parser.add_argument("--require-container", action="store_true")
    parser.add_argument("--require-signal", action="store_true")
    parser.add_argument("--self-signal", action="store_true")
    parser.add_argument("--repl", action="store_true")
    args = parser.parse_args(argv)
    if not __debug__:
        parser.error("验收探针需要断言；不能使用 -O。")
    if args.action == "smoke":
        result = smoke(args.state_dir, args.workspace_dir, require_container=args.require_container)
    elif args.action == "verify":
        result = verify(args.state_dir, args.workspace_dir, require_signal=args.require_signal)
    else:
        result = signal_task(args.state_dir, args.workspace_dir, self_signal=args.self_signal, repl=args.repl)
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(result.get("exit_code", 0 if result["status"] == "passed" else 1))


if __name__ == "__main__":
    main()
