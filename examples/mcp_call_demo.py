"""固定模型调用真实外部工具；执行与权限真实，模型回答预设。"""

import json
from threading import get_ident

from personal_agent.agent import run_agent_loop
from personal_agent.mcp_client import PROJECT_ROOT
from personal_agent.mcp_runtime import McpToolRuntime, load_mcp_config
from personal_agent.plan import PlanSession
from personal_agent.tool_registry import execute_tool


def run_demo() -> dict:
    approvals, request_count = [], 0
    owner = get_ident()
    history = [{"role": "user", "content": "请通过 MCP 回显：你好，外部工具。"}]

    def approve(name, arguments, reason):
        # 演示只批准这个明确的无副作用 echo；不用于批准任意真实外部工具。
        approved = name == "mcp__demo__echo" and arguments == {"text": "你好，外部工具。"}
        approvals.append({"name": name, "arguments": arguments, "approved": approved, "main_thread": get_ident() == owner})
        return approved

    with McpToolRuntime(load_mcp_config(PROJECT_ROOT / "examples/mcp-config.json")) as runtime:
        def model(messages, definitions):
            nonlocal request_count
            request_count += 1
            # 复用聊天层的适配方式；模型看到规范化工具名和参数 Schema。
            available = [*definitions, *runtime.definitions()]
            assert any(item["name"] == "mcp__demo__echo" for item in available)
            if request_count == 1:
                return {"role": "assistant", "tool_calls": [{
                    "id": "mcp_echo_1", "type": "function", "function": {
                        "name": "mcp__demo__echo", "arguments": json.dumps({"text": "你好，外部工具。"}, ensure_ascii=False),
                    },
                }]}
            assert "你好，外部工具。" in messages[-1]["content"]
            return {"role": "assistant", "content": "外部 echo 已返回：你好，外部工具。"}

        denied = execute_tool("mcp__demo__echo", {"text": "禁止执行"}, mcp=runtime, plan=PlanSession("plan"), allow=["mcp__demo__echo"])
        run_agent_loop(history, model, tool_executor=lambda name, arguments: execute_tool(name, arguments, mcp=runtime, confirm=approve))
        invalid = execute_tool("mcp__demo__echo", {"text": 123}, mcp=runtime, allow=["mcp__demo__echo"])
        clients = list(runtime.clients.values())
    return {
        "validation": "fixed model; actual stdio discovery/list/call and permission checks; no live model request",
        "scripted_model_requests": request_count, "approvals": approvals, "history": history,
        "plan_denial": denied, "server_tool_error": invalid,
        "servers": [{"pid": client.pid, "returncode_after_close": client.returncode, "stderr": client.stderr_tail} for client in clients],
    }


if __name__ == "__main__":
    print(json.dumps(run_demo(), ensure_ascii=False, indent=2))
