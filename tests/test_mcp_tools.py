"""两条工具列表流程：真实 Server 与分页，以及坏工具描述的明确拒绝。"""

import json
import sys
import unittest

from personal_agent.mcp_client import McpProtocolError, McpRemoteError, StdioMcpClient


SERVER_COMMAND = [sys.executable, "-X", "utf8", "-B", "-u", "-m", "examples.mcp_server_demo"]


def fixture_command(results):
    # 子进程独立校验收到的元信息和游标，避免只测试预造的 Python 对象。
    source = '''
import json, sys
results = json.loads(sys.argv[1])
expected_cursor = None
for index, result in enumerate(results):
    request = json.loads(sys.stdin.buffer.readline())
    assert request['method'] == 'tools/list'
    assert request['id'] == index + 1
    params = request['params']
    meta = params['_meta']
    assert meta['io.modelcontextprotocol/protocolVersion'] == '2026-07-28'
    assert meta['io.modelcontextprotocol/clientCapabilities'] == {}
    assert meta['io.modelcontextprotocol/clientInfo']['name'] == 'personal-agent'
    if expected_cursor is None:
        assert 'cursor' not in params
    else:
        assert params['cursor'] == expected_cursor
    print(json.dumps({'jsonrpc':'2.0', 'id':request['id'], 'result':result}), flush=True)
    expected_cursor = result.get('nextCursor')
'''
    return [sys.executable, "-X", "utf8", "-B", "-u", "-c", source, json.dumps(results)]


class McpToolsSmokeTests(unittest.TestCase):
    def test_real_listing_and_explicit_pagination_preserve_metadata_and_empty_cursor(self):
        client = StdioMcpClient(SERVER_COMMAND)
        with client:
            discovery = client.discover()
            page = client.list_tools()
            self.assertEqual(discovery.capabilities, {"tools": {}})
            self.assertEqual(len(page.tools), 1)
            tool = page.tools[0]
            self.assertEqual((tool.name, tool.description), ("echo", "回显输入的文本。"))
            self.assertEqual(tool.input_schema["properties"]["text"], {"type": "string"})
            self.assertEqual(tool.input_schema["required"], ["text"])
            self.assertIs(tool.input_schema["additionalProperties"], False)
            self.assertIsNone(page.next_cursor)
            self.assertEqual((page.ttl_ms, page.cache_scope), (0, "public"))
            self.assertEqual(client.list_tools(), page)
            with self.assertRaises(McpRemoteError) as raised:
                client.list_tools("invalid-cursor")
            self.assertEqual(raised.exception.code, -32602)
        self.assertEqual(client.returncode, 0)
        self.assertIn("tools/list id=2", client.stderr_tail)
        self.assertIn("tools/list id=3", client.stderr_tail)
        self.assertNotIn("tools/call", client.stderr_tail)

        results = [
            {"resultType": "complete", "tools": [{"name": "inspect", "inputSchema": {"type": "object"}}], "nextCursor": "", "ttlMs": 0, "cacheScope": "public"},
            {"resultType": "complete", "tools": [], "ttlMs": 0, "cacheScope": "public"},
        ]
        pages = StdioMcpClient(fixture_command(results))
        with pages:
            first = pages.list_tools()
            self.assertEqual(first.tools[0].description, "")
            self.assertEqual(first.next_cursor, "")
            final = pages.list_tools(first.next_cursor)
            self.assertEqual(final.tools, ())
            self.assertIsNone(final.next_cursor)
        self.assertEqual(pages.returncode, 0)

    def test_invalid_tool_pages_are_not_silently_converted_to_empty_lists(self):
        tool = {"name": "echo", "inputSchema": {"type": "object"}}
        cases = [
            ({"tools": {}}, "tools 数组"),
            ({"tools": [tool, tool]}, "name 重复"),
            ({"tools": [{"name": "broken", "inputSchema": None}]}, "inputSchema"),
            ({"tools": [], "nextCursor": 9}, "nextCursor"),
        ]
        for fields, message in cases:
            with self.subTest(message=message):
                result = {"resultType": "complete", "ttlMs": 0, "cacheScope": "public", **fields}
                client = StdioMcpClient(fixture_command([result]))
                with self.assertRaisesRegex(McpProtocolError, message):
                    with client:
                        client.list_tools()
                self.assertEqual(client.returncode, 0)
