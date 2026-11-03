"""固定模型响应的 explore 演示：实际读取项目源码，不请求模型服务。"""

import copy
import json

from personal_agent.subagent import run_explore_agent
from personal_agent.tool_registry import execute_tool


def run_demo() -> dict:
    parent_messages = [{"role": "user", "content": "了解 frontmatter 解析流程，先调查入口。"}]
    before = copy.deepcopy(parent_messages)
    task = "读取 personal_agent/frontmatter.py 前 20 行，说明解析入口。"
    calls = [
        {"id": "read_entry", "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"file_path": "personal_agent/frontmatter.py", "limit": 20})}},
        {"id": "blocked_write", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"file_path": "playground/explore-must-not-write.txt", "content": "这段内容不应写入"})}},
    ]
    responses = iter([
        {"role": "assistant", "tool_calls": calls},
        {"role": "assistant", "content": "固定演示结论：已读取 frontmatter.py 的解析入口；写入请求被 explore 拦截。"},
    ])
    requests, visible_tools, dispatched_tools = [], [], []

    def model(messages, definitions):
        requests.append(copy.deepcopy(messages))
        visible_tools.append(sorted(item["name"] for item in definitions))
        return next(responses)

    def dispatch(name, arguments):
        dispatched_tools.append(name)
        return execute_tool(name, arguments, mode="acceptEdits")

    result = run_explore_agent(task, model, dispatch, max_turns=2)
    tool_results = [item for item in result.messages if item["role"] == "tool"]
    assert parent_messages == before
    assert dispatched_tools == ["read_file"]
    assert all(names == ["grep_search", "list_files", "read_file"] for names in visible_tools)
    assert "def parse_frontmatter" in tool_results[0]["content"]
    assert "explore 仅允许" in tool_results[1]["content"]
    return {
        "verification": "fixed model responses; real file read; no model service requests",
        "task": task,
        "logical_model_calls": len(requests),
        "live_model_requests": 0,
        "visible_tools_per_request": visible_tools,
        "dispatched_tools": dispatched_tools,
        "parent_history_unchanged": parent_messages == before,
        "parent_message_count": len(parent_messages),
        "child_message_count": len(result.messages),
        "result": result.text,
        "child_messages": result.messages,
    }


def main() -> None:
    print(json.dumps(run_demo(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
