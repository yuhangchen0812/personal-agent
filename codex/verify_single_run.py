"""固定模型验证单次 CLI/启动脚本；--live 另执行当前配置的真实只读任务。"""

import argparse
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from personal_agent.main import main
from personal_agent.session import Session
from personal_agent.single_run import run_once


def invoke(arguments):
    output, errors = io.StringIO(), io.StringIO()
    code = 0
    with redirect_stdout(output), redirect_stderr(errors):
        try:
            main(arguments)
        except SystemExit as exit_status:
            code = exit_status.code
    return code, output.getvalue(), errors.getvalue()


def tool(call_id, name, arguments):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


def launcher_checks():
    observations = []
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary).resolve()
        child_environment = {**os.environ, "PERSONAL_AGENT_PROTOCOL": "openai", "PERSONAL_AGENT_MODEL": "offline-launcher"}
        prompt = directory / "任务 输入.txt"
        prompt.write_text('中文任务\n保留 "引号" 和换行。', encoding="utf-8")
        cmd = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
        # 命令只含本项目启动路径、固定参数与本脚本生成的文件名，不嵌入提示正文。
        cmdline = f'"{cmd}" /d /s /c ""{ROOT / "run.cmd"}" --prompt-file "{prompt.name}" --output-format json --input-price 1 --output-price 1 --max-cost 0"'
        script_path = str(ROOT / 'run.cmd').replace("'", "''")
        pscommand = ("[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false); "
                     f"& '{script_path}' --prompt-file '{prompt.name}' --output-format json --input-price 1 --output-price 1 --max-cost 0; exit $LASTEXITCODE")
        for name, command in (("cmd", cmdline), ("powershell", ['powershell.exe', '-NoProfile', '-Command', pscommand])):
            result = subprocess.run(command, cwd=directory, env=child_environment, capture_output=True, timeout=30)
            stdout = result.stdout.decode("utf-8-sig")
            assert stdout.strip(), (name, result.returncode, result.stderr.decode("utf-8", errors="replace")[:800])
            report = json.loads(stdout)
            assert result.returncode == report["exit_code"] == 1
            assert report["status"] == "failed" and report["usage"]["calls"] == 0
            assert "预算" in report["error"]["message"]
            observations.append({"launcher": name, "cwd_outside_project": True, "relative_utf8_prompt_file": True,
                                 "json_stdout_valid": True, "exit_code": result.returncode, "model_calls": 0})
        module = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-m', 'personal_agent', '--help'], cwd=ROOT, capture_output=True, timeout=30)
        assert module.returncode == 0 and b'--prompt-file' in module.stdout
    return observations


def verify_offline():
    requests = []
    def model(messages, definitions=None, *, on_usage=None, **_):
        requests.append(deepcopy(messages))
        on_usage({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        if len(requests) == 1:
            return {"role": "assistant", "content": None, "tool_calls": [
                tool("read", "read_file", {"file_path": "playground/source.txt"}),
                tool("denied", "write_file", {"file_path": "playground/blocked.txt", "content": "不得提前批准"})]}
        return {"role": "assistant", "content": "已读取中文材料；写入未获批准。"}

    def streamed(messages, definitions=None, *, on_text=None, on_usage=None, **_):
        result = model(messages, definitions, on_usage=on_usage)
        on_text(result["content"][:5])
        on_text(result["content"][5:])
        return result

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        (root / "playground").mkdir()
        (root / "playground/source.txt").write_text("实际中文材料", encoding="utf-8")
        prompt = root / "多行任务.txt"
        prompt.write_text('读取材料\n不要猜测 "引号"。', encoding="utf-8-sig")
        with (
            patch("personal_agent.main.get_model_settings", return_value={"protocol": "openai", "model": "fixed-model", "base_url": "https://example.invalid"}),
            patch("personal_agent.main.get_model_message", side_effect=model),
            patch("personal_agent.main.stream_model_message", side_effect=streamed),
            patch("personal_agent.tools.PROJECT_ROOT", root), patch("personal_agent.session.PROJECT_ROOT", root),
            patch("personal_agent.main.discover_skills", return_value=([], [])),
            patch("personal_agent.main.discover_agents", return_value=([], [])),
            patch("builtins.input", side_effect=AssertionError("single run must not read terminal input")) as terminal,
        ):
            code, output, errors = invoke(["--prompt-file", str(prompt), "--output-format", "json", "--session", "once", "--with-tools"])
            report = json.loads(output)
            assert code == 0 and report["status"] == "completed" and report["tool_error_count"] == 1
            assert "实际中文材料" in report["tool_trace"][0]["result_preview"]
            assert report["usage"]["total_tokens"] == 30 and report["session"]["saved"] is True
            assert requests[0][-1]["content"] == '读取材料\n不要猜测 "引号"。'
            before = Session("once").load()
            code, text, _ = invoke(["--prompt", "/clear", "--resume", "once"])
            assert code == 0 and text == "已读取中文材料；写入未获批准。\n"
            assert requests[-1][-1]["content"] == "/clear"
            after = Session("once").load()
            assert after[:len(before)] == before and len(after) == len(before) + 2
            code, stream_output, stream_logs = invoke(["--prompt", "exit", "--stream", "--output-format", "json"])
            assert code == 0 and json.loads(stream_output)["answer"] == "已读取中文材料；写入未获批准。"
            assert "Personal Agent > " in stream_logs and requests[-1][-1]["content"] == "exit"
            terminal.assert_not_called()
        assert not (root / "playground/blocked.txt").exists()
        assert "Personal Agent 单次任务已启动" not in output and "Personal Agent 单次任务已启动" in errors
    return {"status": "passed", "validation": "fixed model callbacks / real CLI, read_file, permission rejection and session files",
            "json_report": report, "resume_kept_history": True, "slash_prompt_is_literal": True,
            "stdout_clean": True, "terminal_input_calls": 0, "model_requests": len(requests),
            "stream_stdout_clean": True, "exit_prompt_is_literal": True,
            "launchers": launcher_checks()}


def verify_live():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        (root / "playground").mkdir()
        (root / "playground/once.txt").write_text("单次任务验证-C10", encoding="utf-8")
        with patch("personal_agent.tools.PROJECT_ROOT", root), patch("personal_agent.main.discover_skills", return_value=([], [])):
            report = run_once("调用 read_file 读取 playground/once.txt，只回复文件中的验证词。", max_turns=3,
                              permission_mode="dontAsk", deny=["agent", "skill", "run_shell", "write_file", "edit_file", "enter_plan_mode", "exit_plan_mode", "compact_context"])
        assert report["status"] == "completed" and "单次任务验证-C10" in report["answer"]
        assert any(row["name"] == "read_file" for row in report["tool_trace"])
    return {"validation": "real current model API / real read_file / isolated temporary file", "report": report}


def main_script():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    report = {"date": "2026-10-29", "offline": verify_offline(), "live": verify_live() if args.live else {"status": "not_run"}}
    target = ROOT / "codex" / "single-run-check-2026-10-29.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(target)


if __name__ == "__main__":
    main_script()
