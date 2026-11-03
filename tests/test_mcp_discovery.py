"""两条真实子进程流程：发现与正常关闭，以及协议/传输失败清理。"""

import sys
import unittest
from unittest.mock import patch

from personal_agent.mcp_client import (
    MCP_PROTOCOL_VERSION, PROJECT_ROOT, McpProtocolError, McpRemoteError,
    McpTransportError, StdioMcpClient,
)


SERVER_COMMAND = [sys.executable, "-X", "utf8", "-B", "-u", "-m", "examples.mcp_server_demo"]


class McpDiscoverySmokeTests(unittest.TestCase):
    def test_real_server_repeated_discovery_and_clean_eof_shutdown(self):
        client = StdioMcpClient(SERVER_COMMAND, cwd=PROJECT_ROOT)
        with client:
            first = client.discover()
            pid = client.pid
            second = client.discover()
            self.assertEqual(client.pid, pid)
            self.assertIsNone(client.returncode)
            self.assertEqual(first, second)
            self.assertEqual(first.protocol_version, "2026-07-28")
            self.assertEqual(first.supported_versions, (MCP_PROTOCOL_VERSION,))
            self.assertEqual(first.capabilities, {"tools": {}})
            self.assertEqual(first.server_info, {"name": "personal-agent-demo", "version": "0.1.0"})
            self.assertEqual((first.ttl_ms, first.cache_scope), (0, "public"))
        self.assertEqual(client.returncode, 0)
        self.assertIn("Server 已启动", client.stderr_tail)
        self.assertIn("server/discover id=1", client.stderr_tail)
        self.assertIn("server/discover id=2", client.stderr_tail)
        self.assertIn("stdin EOF", client.stderr_tail)
        with self.assertRaisesRegex(McpTransportError, "已经关闭"):
            client.discover()
        client.close()  # 已结束的连接再次清理不重启或重发请求。

    def test_wrong_version_and_broken_servers_fail_without_leaving_a_process(self):
        # 只改父客户端的版本；真实子进程仍从文件读取当前版本并返回标准错误。
        client = StdioMcpClient(SERVER_COMMAND)
        with patch("personal_agent.mcp_client.MCP_PROTOCOL_VERSION", "2025-11-25"):
            with self.assertRaises(McpRemoteError) as raised:
                with client:
                    client.discover()
        self.assertEqual(raised.exception.code, -32022)
        self.assertEqual(raised.exception.data, {"supported": [MCP_PROTOCOL_VERSION], "requested": "2025-11-25"})
        self.assertEqual(client.returncode, 0)

        fixtures = [
            ("stdout 混入日志", "print('not-json', flush=True)", McpProtocolError, "stdout 必须"),
            ("提前关闭", "pass", McpTransportError, "stdout 已关闭"),
            ("ID 错配", "print(json.dumps({'jsonrpc':'2.0','id':999,'result':{'resultType':'complete'}}), flush=True)", McpProtocolError, "ID"),
            ("不响应", "time.sleep(10)", McpTransportError, "超时"),
        ]
        for label, behavior, error, message in fixtures:
            with self.subTest(case=label):
                source = "import sys,json,time\nsys.stdin.buffer.readline()\n" + behavior
                broken = StdioMcpClient([sys.executable, "-X", "utf8", "-B", "-u", "-c", source], timeout=1)
                with self.assertRaisesRegex(error, message):
                    with broken:
                        broken.discover()
                self.assertIsNotNone(broken.returncode)
