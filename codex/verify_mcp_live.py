"""真实模型 -> 终端审批边界 -> 本地 MCP echo -> 真实回答；最多三次模型请求。"""

import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from personal_agent.config import load_model_config
from personal_agent.llm import get_model_message
from personal_agent.main import run_repl
from personal_agent.mcp_runtime import McpToolRuntime
from personal_agent.permissions import SUPPORTED_TOOLS
from personal_agent.session import Session


def main():
    config = load_model_config()  # 只报告是否存在，不输出密钥或整个配置。
    expected = "松果-MCP-20261028"
    prompt = f"请只调用一次 mcp__demo__echo，参数 text 的值精确为 {expected}，随后依据工具实际返回简短回复该文本。不要调用其他工具。"
    stdout, stderr = io.StringIO(), io.StringIO()
    session_name = "mcp_live_" + uuid4().hex[:8]
    runtimes, approvals, requests = [], [], []

    def create_runtime(configs):
        runtime = McpToolRuntime(configs)
        runtimes.append(runtime)
        return runtime

    def approve(name, arguments, reason):
        accepted = not approvals and name == "mcp__demo__echo" and arguments == {"text": expected}
        approvals.append({"name": name, "arguments": arguments, "accepted": accepted})
        return accepted

    def model(messages, definitions, *, on_usage):
        observed = {"messages_count": len(messages), "mcp_tools": [item["name"] for item in definitions if item["name"].startswith("mcp__")]}
        requests.append(observed)

        def capture(usage):
            observed["usage"] = usage
            on_usage(usage)

        return get_model_message(messages, definitions, on_usage=capture)

    with patch("personal_agent.main.McpToolRuntime", side_effect=create_runtime), \
         patch("personal_agent.main.get_model_message", side_effect=model), \
         patch("personal_agent.main.confirm_in_terminal", side_effect=approve), \
         patch("builtins.input", side_effect=[prompt, "exit"]), \
         redirect_stdout(stdout), redirect_stderr(stderr):
        history = run_repl(max_turns=3, deny=sorted(SUPPORTED_TOOLS), mcp_config=ROOT / "examples/mcp-config.json", mcp_eager=True, session_name=session_name, with_tools=True)
        saved = Session(session_name).load()
        with patch("builtins.input", side_effect=["exit"]):
            restored = run_repl(max_turns=3, deny=sorted(SUPPORTED_TOOLS), mcp_config=ROOT / "examples/mcp-config.json", mcp_eager=True, session_name=session_name, resume=True)
    results = [message for message in history if message["role"] == "tool"]
    clients = [client for runtime in runtimes for client in runtime.clients.values()]
    success = (
        len(results) == 1 and expected in results[0]["content"]
        and not results[0]["content"].startswith("工具调用失败：")
        and len(approvals) == 1 and approvals[0]["accepted"]
        and history[-1]["role"] == "assistant" and expected in history[-1]["content"]
        and saved == history == restored and len(runtimes) == 2
        and "tools/call" not in runtimes[1].clients["demo"].stderr_tail
        and all(client.returncode == 0 for client in clients)
    )
    report = {
        "validation": "live configured model, actual run_repl and stdio MCP echo; approval callback limited to one exact echo",
        "api_key_present": bool(config["api_key"]), "model": config["model"],
        "requests": requests, "approvals": approvals, "history": history,
        "session_name": session_name, "saved_history_matches": saved == history,
        "restored_history_matches": restored == history, "restore_replayed_tool": "tools/call" in runtimes[1].clients["demo"].stderr_tail,
        "servers": [{"pid": client.pid, "returncode_after_close": client.returncode, "stderr": client.stderr_tail} for client in clients],
        "stdout": stdout.getvalue(), "stderr": stderr.getvalue(), "success": success,
    }
    path = ROOT / "codex/mcp-execution-live-check-2026-10-28.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"report": str(path), "model": config["model"], "model_requests": len(requests), "success": success}, ensure_ascii=False))
    if not success:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
