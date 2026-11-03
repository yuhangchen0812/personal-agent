"""可控 MCP 2026-07-28 Server，提供发现、列表及 echo 调用。"""

import json
import sys

from personal_agent.mcp_client import (
    CLIENT_CAPABILITIES_META, MCP_PROTOCOL_VERSION, PROTOCOL_VERSION_META, SERVER_INFO_META,
)


DEMO_TOOL = {
    "name": "echo",
    "description": "回显输入的文本。",
    "inputSchema": {
        "type": "object", "properties": {"text": {"type": "string"}},
        "required": ["text"], "additionalProperties": False,
    },
}


def error_response(request_id, code, message, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    response = {"jsonrpc": "2.0", "error": error}
    if request_id is not None:
        response["id"] = request_id
    return response


def handle_request(request):
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
        return error_response(None, -32600, "Expected a JSON-RPC 2.0 request")
    if "id" not in request:
        return None  # 通知没有响应；本步不实现通知功能。
    request_id = request["id"]
    if type(request_id) not in (str, int):
        return error_response(None, -32600, "Request ID must be a string or integer")
    params = request.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    if not isinstance(meta, dict) or not isinstance(meta.get(PROTOCOL_VERSION_META), str) or not isinstance(meta.get(CLIENT_CAPABILITIES_META), dict):
        return error_response(request_id, -32602, f"MCP {MCP_PROTOCOL_VERSION} requires per-request version and clientCapabilities")
    version = meta[PROTOCOL_VERSION_META]
    if version != MCP_PROTOCOL_VERSION:
        return error_response(request_id, -32022, "Unsupported protocol version", {"supported": [MCP_PROTOCOL_VERSION], "requested": version})
    method = request["method"]
    if method == "server/discover":
        if set(params) != {"_meta"}:
            return error_response(request_id, -32602, "server/discover accepts only standard metadata")
        result = {"supportedVersions": [MCP_PROTOCOL_VERSION], "capabilities": {"tools": {}}}
    elif method == "tools/list":
        if set(params) - {"_meta", "cursor"}:
            return error_response(request_id, -32602, "Unexpected tools/list parameters")
        if "cursor" in params:
            return error_response(request_id, -32602, "This demo has one page; cursor is not valid")
        result = {"tools": [DEMO_TOOL]}
    elif method == "tools/call":
        if set(params) - {"_meta", "name", "arguments"} or not isinstance(params.get("name"), str) or not isinstance(params.get("arguments", {}), dict):
            return error_response(request_id, -32602, "Expected tool name and object arguments")
        if params["name"] != "echo":
            return error_response(request_id, -32602, "Unknown tool: " + params["name"])
        arguments = params.get("arguments", {})
        if set(arguments) != {"text"} or not isinstance(arguments["text"], str):
            result = {"content": [{"type": "text", "text": "echo requires exactly one string argument: text"}], "isError": True}
        else:
            result = {"content": [{"type": "text", "text": arguments["text"]}], "isError": False, "structuredContent": {"text": arguments["text"]}}
    else:
        return error_response(request_id, -32601, f"Unknown method (MCP {MCP_PROTOCOL_VERSION})")
    return {
        "jsonrpc": "2.0", "id": request_id,
        "result": {
            "resultType": "complete", **result,
            "_meta": {SERVER_INFO_META: {"name": "personal-agent-demo", "version": "0.1.0"}},
            "ttlMs": 0, "cacheScope": "public",
        },
    }


def log(text):
    sys.stderr.buffer.write((text + "\n").encode("utf-8"))
    sys.stderr.buffer.flush()


def main():
    log("测试 Server 已启动；stdout 仅传协议消息。")
    for line in sys.stdin.buffer:
        try:
            request = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeError):
            response = error_response(None, -32700, "Invalid UTF-8 JSON")
        else:
            response = handle_request(request)
            if isinstance(request, dict):
                log(f"请求：{request.get('method')} id={request.get('id')}")
        if response is not None:
            sys.stdout.buffer.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
            sys.stdout.buffer.flush()
    log("收到 stdin EOF，测试 Server 正常退出。")


if __name__ == "__main__":
    main()
