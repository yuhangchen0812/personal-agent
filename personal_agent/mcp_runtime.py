"""显式配置的 stdio Server 集合；适配模型工具名，不修改全局注册表。"""

import hashlib
import json
import math
import re
import sys
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from .mcp_client import McpError, McpProtocolError, StdioMcpClient


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    command: list[str]
    cwd: Path
    timeout: float


def load_mcp_config(path: str | Path) -> tuple[McpServerConfig, ...]:
    """只读取调用方指定的配置；相对 cwd 以配置目录为基准，{python} 指当前解释器。"""
    path = Path(path).expanduser().resolve()
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise ValueError(f"无法读取 MCP 配置（{type(error).__name__}）。") from None
    if not isinstance(config, dict) or set(config) != {"mcpServers"} or not isinstance(config["mcpServers"], dict):
        raise ValueError("MCP 配置须为仅含 mcpServers 对象的 JSON。")
    servers = []
    for name, raw in config["mcpServers"].items():
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", name) or "__" in name:
            raise ValueError("MCP Server 名须为 1～24 个字母、数字、下划线或短横线，不能含双下划线。")
        if not isinstance(raw, dict) or set(raw) - {"command", "args", "cwd", "timeout"}:
            raise ValueError(f"Server {name} 只支持 command/args/cwd/timeout。")
        command, args = raw.get("command"), raw.get("args", [])
        cwd, timeout = raw.get("cwd", "."), raw.get("timeout", 5)
        if not isinstance(command, str) or not command.strip() or not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
            raise ValueError(f"Server {name} 须提供非空 command 和字符串 args 数组。")
        if not isinstance(cwd, str) or not cwd.strip():
            raise ValueError(f"Server {name} 的 cwd 必须是非空路径。")
        cwd = (path.parent / Path(cwd).expanduser()).resolve()
        if not cwd.is_dir():
            raise ValueError(f"Server {name} 的 cwd 不是已有目录。")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError(f"Server {name} 的 timeout 必须是正的有限秒数。")
        if command == "{python}":
            command = sys.executable
        elif "/" in command or "\\" in command:
            command = str((cwd / Path(command).expanduser()).resolve())
        servers.append(McpServerConfig(name, [command, *args], cwd, float(timeout)))
    return tuple(servers)


def model_tool_name(server: str, tool: str) -> str:
    """适配 Chat Completions 的 64 字符工具名；改写/截断时附原名摘要。"""
    raw = f"mcp__{server}__{tool}"
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", raw)
    if safe != raw or len(safe) > 64:
        suffix = "__" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
        safe = safe[:64 - len(suffix)] + suffix
    return safe


class McpToolRuntime:
    """启动时发现完整工具快照，主线程顺序调用；退出或启动失败均关闭已启动进程。"""

    def __init__(self, configs: tuple[McpServerConfig, ...]):
        self.configs = configs
        self.clients: dict[str, StdioMcpClient] = {}
        self._definitions: list[dict] = []
        self._routes: dict[str, tuple[StdioMcpClient, str]] = {}
        self._resources = ExitStack()

    @property
    def names(self) -> set[str]:
        return set(self._routes)

    def __enter__(self):
        try:
            for config in self.configs:
                client = self._resources.enter_context(StdioMcpClient(config.command, cwd=config.cwd, timeout=config.timeout))
                self.clients[config.name] = client
                discovery = client.discover()
                if "tools" not in discovery.capabilities:
                    continue
                if not isinstance(discovery.capabilities["tools"], dict):
                    raise McpProtocolError("Server tools 能力必须是对象。")
                cursor, seen_cursors, seen_names = None, set(), set()
                for _ in range(100):
                    page = client.list_tools(cursor)
                    for tool in page.tools:
                        name = model_tool_name(config.name, tool.name)
                        if tool.name in seen_names or name in self._routes:
                            raise McpProtocolError("MCP 工具名称重复，无法建立唯一路由。")
                        seen_names.add(tool.name)
                        self._routes[name] = (client, tool.name)
                        self._definitions.append({
                            "name": name,
                            "description": f"外部 MCP 工具 [{config.name}/{tool.name}]；操作须经过当前权限检查。\n{tool.description}",
                            "input_schema": deepcopy(tool.input_schema),
                        })
                    cursor = page.next_cursor
                    if cursor is None:
                        break
                    if cursor in seen_cursors:
                        raise McpProtocolError("MCP 工具列表游标重复。")
                    seen_cursors.add(cursor)
                else:
                    raise McpProtocolError("MCP 工具列表超过本演示的 100 页上限。")
            return self
        except BaseException:
            self._resources.close()
            raise

    def __exit__(self, *exc):
        return self._resources.__exit__(*exc)

    def definitions(self) -> list[dict]:
        return deepcopy(self._definitions)

    def call(self, name: str, arguments: dict) -> str:
        """只路由已发现的精确名称；由 execute_tool 在调用本方法前完成审批。"""
        route = self._routes.get(name)
        if route is None:
            return "工具调用失败：未知 MCP 工具。"
        client, original_name = route
        try:
            result = client.call_tool(original_name, arguments)
        except (McpError, ValueError) as error:
            return f"工具调用失败：{error}"
        text = result.as_text()
        return f"工具调用失败：MCP 工具返回错误：{text}" if result.is_error else text
