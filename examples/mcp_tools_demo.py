"""真实启动本地 MCP Server，发现服务并列出工具描述；不执行工具。"""

import json
import sys
from dataclasses import asdict

from personal_agent.mcp_client import PROJECT_ROOT, StdioMcpClient


def run_demo() -> dict:
    command = [sys.executable, "-X", "utf8", "-B", "-u", "-m", "examples.mcp_server_demo"]
    client = StdioMcpClient(command, cwd=PROJECT_ROOT)
    with client:
        discovered = client.discover()
        page = client.list_tools()
        pid = client.pid
    return {
        "validation": "actual local stdio discovery and tools/list; no model request or tool execution",
        "discovery": asdict(discovered), "tools_page": asdict(page), "server_pid": pid,
        "returncode_after_close": client.returncode, "server_stderr": client.stderr_tail,
    }


if __name__ == "__main__":
    print(json.dumps(run_demo(), ensure_ascii=False, indent=2))
