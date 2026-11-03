"""MCP 2026-07-28 stdio 客户端：发现、列表、工具调用及进程清理。"""

import json
import math
import re
import subprocess
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from time import monotonic


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_PROTOCOL_VERSION = "2026-07-28"
PROTOCOL_VERSION_META = "io.modelcontextprotocol/protocolVersion"
CLIENT_CAPABILITIES_META = "io.modelcontextprotocol/clientCapabilities"
CLIENT_INFO_META = "io.modelcontextprotocol/clientInfo"
SERVER_INFO_META = "io.modelcontextprotocol/serverInfo"


class McpError(RuntimeError):
    """MCP 客户端的基础错误。"""


class McpTransportError(McpError):
    """本地启动、管道关闭或响应等待超时。"""


class McpProtocolError(McpError):
    """响应不是本步支持的协议结构。"""


class McpRemoteError(McpError):
    def __init__(self, code: int, message: str, data=None):
        super().__init__(f"MCP 服务端错误（{code}）：{message}")
        self.code = code
        self.data = data


def _reject_json_constant(value: str):
    raise ValueError(f"Invalid JSON constant: {value}")


@dataclass(frozen=True)
class McpDiscovery:
    protocol_version: str
    supported_versions: tuple[str, ...]
    capabilities: dict
    server_info: dict | None
    ttl_ms: int
    cache_scope: str


@dataclass(frozen=True)
class McpToolDefinition:
    name: str
    description: str
    input_schema: dict


@dataclass(frozen=True)
class McpToolsPage:
    tools: tuple[McpToolDefinition, ...]
    next_cursor: str | None
    ttl_ms: int
    cache_scope: str


@dataclass(frozen=True)
class McpToolResult:
    content: tuple[dict, ...]
    is_error: bool
    structured_content: object = None
    has_structured_content: bool = False

    def as_text(self) -> str:
        """当前聊天只消费文本；其他块明确标注类型，不把二进制数据交给模型。"""
        parts = [item["text"] if item["type"] == "text" else f"[未展示的 MCP 内容：{item['type']}]" for item in self.content]
        if self.has_structured_content:
            parts.append("structuredContent: " + json.dumps(self.structured_content, ensure_ascii=False))
        return "\n".join(parts) or "（MCP 工具返回空内容）"


def _cache_hints(result: dict) -> tuple[int, str]:
    ttl, scope = result.get("ttlMs"), result.get("cacheScope")
    if type(ttl) is not int or ttl < 0 or scope not in ("public", "private"):
        raise McpProtocolError("缓存提示须含非负整数 ttlMs 和 public/private cacheScope。")
    return ttl, scope


def _parse_tool_definition(raw) -> McpToolDefinition:
    """检查本步消费的字段与 Schema 外层结构，不执行工具或完整 JSON Schema 验证。"""
    if not isinstance(raw, dict):
        raise McpProtocolError("每个工具描述必须是对象。")
    name = raw.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name):
        raise McpProtocolError("工具 name 须为 1～128 个 ASCII 字母、数字、下划线、点或短横线。")
    description = raw.get("description", "")
    if not isinstance(description, str):
        raise McpProtocolError(f"工具 {name} 的 description 必须是字符串。")
    schema = raw.get("inputSchema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise McpProtocolError(f"工具 {name} 的 inputSchema 必须是 type=object 的对象。")
    if "properties" in schema and not isinstance(schema["properties"], dict):
        raise McpProtocolError(f"工具 {name} 的 inputSchema.properties 必须是对象。")
    if "required" in schema:
        required = schema["required"]
        if not isinstance(required, list) or any(not isinstance(item, str) for item in required) or len(set(required)) != len(required):
            raise McpProtocolError(f"工具 {name} 的 inputSchema.required 必须是不重复的字符串列表。")
    return McpToolDefinition(name, description, schema)


class StdioMcpClient:
    """由调用方明确启动一个 Server；主线程顺序请求，不自动重试工具。"""

    def __init__(self, command: list[str], *, cwd: str | Path = PROJECT_ROOT, timeout: float = 5.0):
        if not isinstance(command, list) or not command or any(not isinstance(part, str) or not part for part in command):
            raise ValueError("command 必须是非空字符串列表，不使用 Shell 命令拼接。")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout 必须是正的有限秒数。")
        self.command = list(command)
        self.cwd = Path(cwd).resolve()
        self.timeout = float(timeout)
        self._process: subprocess.Popen | None = None
        self._stdout: Queue[bytes] = Queue()
        self._stderr: deque[str] = deque(maxlen=20)
        self._threads: list[Thread] = []
        self._next_id = 1

    @property
    def pid(self) -> int | None:
        return None if self._process is None else self._process.pid

    @property
    def returncode(self) -> int | None:
        return None if self._process is None else self._process.poll()

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr)

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()

    def start(self):
        if self._process is not None:
            raise McpTransportError("同一客户端实例不能重复启动 Server。")
        try:
            self._process = subprocess.Popen(
                self.command, cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as error:
            raise McpTransportError(f"无法启动 MCP Server（{type(error).__name__}）。") from None
        self._threads = [Thread(target=self._read_stdout, daemon=True), Thread(target=self._read_stderr, daemon=True)]
        for thread in self._threads:
            thread.start()
        return self

    def _read_stdout(self) -> None:
        # 工作线程只收集字节；主线程通过 Queue 有限等待，兼容 Windows 管道。
        try:
            for line in self._process.stdout:
                self._stdout.put(line)
        finally:
            self._stdout.put(b"")  # EOF 也是事件，不能让请求一直等到超时。

    def _read_stderr(self) -> None:
        for line in self._process.stderr:
            self._stderr.append(line.decode("utf-8", errors="replace").rstrip())

    def _request(self, method: str, params: dict | None = None) -> dict:
        try:
            return self._request_once(method, params)
        except (McpTransportError, McpProtocolError, KeyboardInterrupt):
            # 超时后迟到的响应不能被下一次请求误用；中断后也不重放外部操作。
            self.close()
            raise

    def _request_once(self, method: str, params: dict | None = None) -> dict:
        process = self._process
        if process is None or process.stdin.closed or process.poll() is not None:
            raise McpTransportError("MCP Server 尚未启动或已经关闭。")
        request_id = self._next_id
        self._next_id += 1
        request_params = dict(params) if params is not None else {}
        request_params["_meta"] = {
            PROTOCOL_VERSION_META: MCP_PROTOCOL_VERSION,
            CLIENT_CAPABILITIES_META: {},
            CLIENT_INFO_META: {"name": "personal-agent", "version": "0.1.0"},
        }
        request = {
            "jsonrpc": "2.0", "id": request_id, "method": method,
            "params": request_params,
        }
        deadline = monotonic() + self.timeout
        try:
            process.stdin.write((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
            process.stdin.flush()
        except OSError:
            raise McpTransportError("MCP Server 输入管道已关闭。") from None
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise McpTransportError(f"等待 MCP 响应超时（{self.timeout:g} 秒）。")
            try:
                line = self._stdout.get(timeout=remaining)
            except Empty:
                raise McpTransportError(f"等待 MCP 响应超时（{self.timeout:g} 秒）。") from None
            if not line:
                raise McpTransportError("MCP Server stdout 已关闭，未收到对应响应。")
            try:
                message = json.loads(line.decode("utf-8"), parse_constant=_reject_json_constant)
            except (ValueError, UnicodeError):
                raise McpProtocolError("Server stdout 必须是一行一条 UTF-8 JSON-RPC；诊断日志应写到 stderr。") from None
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise McpProtocolError("响应必须是 JSON-RPC 2.0 对象。")
            if "id" not in message and isinstance(message.get("method"), str):
                continue  # 本步不消费通知；不回包，也不因此延长请求期限。
            if type(message.get("id")) is not int or message["id"] != request_id:
                raise McpProtocolError("MCP 响应 ID 与当前请求不匹配。")
            if ("error" in message) == ("result" in message):
                raise McpProtocolError("响应必须且只能包含 result 或 error。")
            if "error" in message:
                error = message["error"]
                if not isinstance(error, dict) or type(error.get("code")) is not int or not isinstance(error.get("message"), str):
                    raise McpProtocolError("JSON-RPC error 必须含整数 code 与字符串 message。")
                raise McpRemoteError(error["code"], error["message"], error.get("data"))
            result = message["result"]
            if not isinstance(result, dict) or result.get("resultType", "complete") != "complete":
                raise McpProtocolError("本步只接受完成的对象结果，不处理多轮输入。")
            return result

    def discover(self) -> McpDiscovery:
        result = self._request("server/discover")
        versions = result.get("supportedVersions")
        if not isinstance(versions, list) or not versions or any(not isinstance(version, str) or not version for version in versions):
            raise McpProtocolError("发现结果须含非空 supportedVersions 字符串列表。")
        if MCP_PROTOCOL_VERSION not in versions:
            raise McpProtocolError(f"本客户端仅支持 MCP {MCP_PROTOCOL_VERSION}，Server 未声明该版本。")
        capabilities, meta = result.get("capabilities"), result.get("_meta", {})
        if not isinstance(capabilities, dict) or not isinstance(meta, dict):
            raise McpProtocolError("capabilities 和 _meta 必须是对象。")
        info = meta.get(SERVER_INFO_META)
        if info is not None and (not isinstance(info, dict) or any(not isinstance(info.get(key), str) or not info[key] for key in ("name", "version"))):
            raise McpProtocolError("serverInfo 须含非空 name 和 version。")
        ttl, scope = _cache_hints(result)
        return McpDiscovery(MCP_PROTOCOL_VERSION, tuple(versions), capabilities, info, ttl, scope)

    def list_tools(self, cursor: str | None = None) -> McpToolsPage:
        """读取一页工具描述；空字符串游标有效，不自动翻页或注册执行。"""
        if cursor is not None and not isinstance(cursor, str):
            raise ValueError("cursor 必须是字符串或 None。")
        result = self._request("tools/list", {"cursor": cursor} if cursor is not None else None)
        raw_tools = result.get("tools")
        if not isinstance(raw_tools, list):
            raise McpProtocolError("tools/list 结果必须包含 tools 数组，空列表须显式返回 []。")
        tools = tuple(_parse_tool_definition(raw) for raw in raw_tools)
        names = [tool.name for tool in tools]
        if len(set(names)) != len(names):
            raise McpProtocolError("同一页工具 name 重复，无法确定调用目标。")
        next_cursor = result.get("nextCursor")
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise McpProtocolError("nextCursor 必须是字符串或 null。")
        ttl, scope = _cache_hints(result)
        return McpToolsPage(tools, next_cursor, ttl, scope)

    def call_tool(self, name: str, arguments: dict) -> McpToolResult:
        """调用一次，区分 JSON-RPC 错误和 isError 工具错误；Schema 由 Server 校验。"""
        if not isinstance(name, str) or not name:
            raise ValueError("工具 name 必须是非空字符串。")
        if not isinstance(arguments, dict):
            raise ValueError("MCP 工具参数必须是 JSON object。")
        try:
            json.dumps(arguments, allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError("MCP 工具参数必须可编码为有限 JSON 值。") from None
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        content, is_error = result.get("content"), result.get("isError", False)
        if not isinstance(content, list) or type(is_error) is not bool:
            raise McpProtocolError("工具结果须含 content 数组，isError 必须为布尔值。")
        for item in content:
            if not isinstance(item, dict) or not isinstance(item.get("type"), str) or item["type"] not in {"text", "image", "audio", "resource", "resource_link"}:
                raise McpProtocolError("工具结果含不支持的内容块类型。")
            if item["type"] == "text" and not isinstance(item.get("text"), str):
                raise McpProtocolError("text 内容块须含字符串 text。")
        return McpToolResult(tuple(content), is_error, result.get("structuredContent"), "structuredContent" in result)

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            process.stdin.close()  # stdin EOF 是 stdio Server 的正常关闭信号。
        except OSError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        for thread in self._threads:
            thread.join(timeout=1)
        process.stdout.close()
        process.stderr.close()
