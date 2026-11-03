"""真实启动本地 MCP 子进程并发现服务；不请求模型、不调用工具。"""

import json
import sys
from dataclasses import asdict

from personal_agent.mcp_client import PROJECT_ROOT, StdioMcpClient


def run_demo() -> dict:
    command = [sys.executable, "-X", "utf8", "-B", "-u", "-m", "examples.mcp_server_demo"]
    client = StdioMcpClient(command, cwd=PROJECT_ROOT)
    with client:
        discovered = client.discover()
        pid = client.pid
    return {
        "validation": "actual local stdio subprocess, no model request or MCP tool execution",
        "discovery": asdict(discovered), "server_pid": pid,
        "returncode_after_close": client.returncode, "server_stderr": client.stderr_tail,
    }


if __name__ == "__main__":
    print(json.dumps(run_demo(), ensure_ascii=False, indent=2))
