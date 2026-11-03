"""Agent Loop 消息流教学：固定响应、真实只读工具，记录成功/拒绝/次数到顶三条轨迹。"""

import argparse
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from personal_agent.agent import run_agent_loop
from personal_agent.file_runtime import plain_path, write_json_atomic
from personal_agent.tool_registry import TOOL_REGISTRY, execute_tool


ROOT = Path(__file__).resolve().parent.parent
SOURCE = "examples/repair-eval/dev-01/project/tags.py"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def run_case(name):
    """调用原 Agent Loop；deepcopy 只用于记录快照，不替换循环中的历史对象。"""
    require(name in {"success", "denied", "turn_limit"}, "未知教学案例。")
    messages = [{"role": "user", "content": "读取标签函数，说明当前返回语句。"}]
    initial = deepcopy(messages)
    events, requests, dispatches, handler_calls = [], [], [], []
    original_handler = TOOL_REGISTRY["read_file"]["handler"]

    def model(history, definitions):
        require(history is messages, "模型应接收调用者传入的同一个历史列表。")
        require(any(item["name"] == "read_file" for item in definitions), "缺少 read_file 定义。")
        snapshot = deepcopy(history)
        requests.append(snapshot)
        events.append({"event": "model_request", "round": len(requests), "messages": snapshot})
        if len(requests) == 1:
            response = {"role": "assistant", "tool_calls": [{"id": "read_tag_1", "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"file_path": SOURCE}, ensure_ascii=False)}}]}
        else:
            require(len(requests) == 2 and history[-1]["role"] == "tool", "第二次请求应带上工具结果。")
            result = history[-1]["content"]
            require(history[-1]["tool_call_id"] == "read_tag_1", "工具结果没有配对原调用 ID。")
            if name == "denied":
                require("权限拒绝" in result, "显式 deny 没有返回拒绝结果。")
                text = "固定回答：读取请求被拒绝，无法根据本次工具结果解释文件内容。"
            else:
                require("def normalize_tag(text):" in result and "return text.lower()" in result, "未读到预期的实际源码。")
                text = "固定回答：已读到 normalize_tag，当前返回语句是 return text.lower()。"
            response = {"role": "assistant", "content": text}
        events.append({"event": "model_response", "round": len(requests), "response": deepcopy(response)})
        return response

    def observed_handler(**arguments):
        handler_calls.append(deepcopy(arguments))
        events.append({"event": "read_file_handler_entered", "arguments": deepcopy(arguments)})
        return original_handler(**arguments)

    def dispatch(tool_name, arguments):
        dispatches.append({"name": tool_name, "arguments": deepcopy(arguments)})
        events.append({"event": "executor_input", "name": tool_name, "arguments": deepcopy(arguments),
                       "history_at_dispatch": deepcopy(messages)})
        result = execute_tool(tool_name, arguments, mode="dontAsk", deny=["read_file"] if name == "denied" else None)
        events.append({"event": "executor_result", "content": result})
        return result

    answer, error = None, None
    # 包装原处理函数以计数，仍由 execute_tool 完成参数/权限检查；离开 with 后恢复注册表。
    with patch.dict(TOOL_REGISTRY["read_file"], {"handler": observed_handler}):
        try:
            answer = run_agent_loop(messages, model, max_turns=1 if name == "turn_limit" else 2,
                                    tool_executor=dispatch, max_parallel_reads=1)
        except RuntimeError as caught:
            if name != "turn_limit":
                raise
            error = {"type": type(caught).__name__, "message": str(caught)}
    require(TOOL_REGISTRY["read_file"]["handler"] is original_handler, "观察包装未恢复。")
    require(requests[0] == initial, "第一轮快照受到后续追加影响。")
    require(messages[1]["tool_calls"][0]["id"] == messages[2]["tool_call_id"], "历史中的调用 ID 未配对。")
    if name == "turn_limit":
        require(error is not None and "上限" in error["message"] and answer is None, "应保留次数到顶的异常。")
        require(not dispatches and not handler_calls and "工具未执行" in messages[2]["content"], "次数到顶后不应执行工具。")
        require(len(messages) == 3, "循环异常前追加的消息应仍能在调用者列表中观察到。")
    else:
        require(error is None and answer == messages[-1]["content"], "返回正文与历史中的最终回复不一致。")
        require([len(item) for item in requests] == [1, 3] and len(messages) == 4, "消息没有按预期扩展。")
        require(len(dispatches) == 1 and len(handler_calls) == (1 if name == "success" else 0), "实际处理函数调用次数不一致。")
    return {"case": name, "status": "expected_limit_observed" if error else "completed",
        "max_turns": 1 if name == "turn_limit" else 2, "max_parallel_reads": 1,
        "same_history_object_at_every_request": True, "initial_snapshot_unchanged": requests[0] == initial,
        "request_message_counts": [len(item) for item in requests], "model_callbacks": len(requests),
        "executor_calls": len(dispatches), "read_file_handler_calls": len(handler_calls),
        "initial_messages": initial, "final_messages": deepcopy(messages), "answer": answer, "loop_error": error,
        "events": events, "new_remote_model_calls": 0}


def render_trace(report):
    lines = ["# Agent Loop 实际消息轨迹", "", "固定模型响应，真实文件工具。每个快照来自实际运行；三个案例没有请求远端模型。", "",
        "| 案例 | 每次模型请求的消息数 | 执行入口调用 | 实际 read_file 处理函数 | 最终消息数 |",
        "| --- | --- | --- | --- | --- |"]
    for case in report["cases"]:
        lines.append(f"| {case['case']} | {case['request_message_counts']} | {case['executor_calls']} | {case['read_file_handler_calls']} | {len(case['final_messages'])} |")
    for case in report["cases"]:
        lines += ["", "## " + case["case"], "", "返回值：" + str(case["answer"]), "",
                  "循环异常：" + json.dumps(case["loop_error"], ensure_ascii=False), "", "最终 messages：", "", "```json",
                  json.dumps(case["final_messages"], ensure_ascii=False, indent=2), "```"]
    lines += ["", "逐事件快照见 report.json 中各案例的 events。executor_input 中 arguments 已解析为字典；原 assistant 消息中的 arguments 仍是 JSON 字符串。",
              "", "turn_limit 是预期观察：工具没有执行，循环抛出异常，调用者列表仍含请求与配对错误结果；上层 REPL 的历史回滚是另一步。", ""]
    return "\n".join(lines)


def run_walkthrough(output_dir):
    directory = plain_path(output_dir)
    protected = [ROOT / name for name in ("personal_agent", "examples", "codex", ".git", ".venv")]
    require(not directory.exists() and not ROOT.is_relative_to(directory)
            and not any(directory.is_relative_to(path) for path in protected), "请提供不覆盖源码或旧记录的新目录。")
    directory.mkdir(parents=True, exist_ok=False)
    report = {"version": 1, "kind": "agent_loop_walkthrough", "status": "running", "directory": str(directory),
        "source_file_read": SOURCE, "cases": [], "new_remote_model_calls": 0,
        "boundary": "scripted callbacks and one authored source file; actual loop/registry/permissions/read_file; serial observation, no session persistence or live inference"}
    write_json_atomic(directory / "report.json", report)
    try:
        for name in ("success", "denied", "turn_limit"):
            report["cases"].append(run_case(name))
            write_json_atomic(directory / "report.json", report)
        report["status"] = "passed"
        (directory / "trace.md").write_text(render_trace(report), encoding="utf-8", newline="\n")
        write_json_atomic(directory / "report.json", report)
        return report
    except (Exception, KeyboardInterrupt) as error:
        report.update(status="cancelled" if isinstance(error, KeyboardInterrupt) else "failed", error_type=type(error).__name__)
        write_json_atomic(directory / "report.json", report)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".personal/agent-loop-lessons" / uuid4().hex,
                        help="新的输出目录，保存完整消息和逐事件快照。")
    try:
        report = run_walkthrough(parser.parse_args().output_dir)
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except (ValueError, OSError) as error:
        print(f"教学演示失败：{error}")
        raise SystemExit(1) from None
    for case in report["cases"]:
        print(f"{case['case']}: 模型请求消息数={case['request_message_counts']}, 执行入口={case['executor_calls']}, read_file处理函数={case['read_file_handler_calls']}")
    print("逐步轨迹：" + str(Path(report["directory"]) / "trace.md"))


if __name__ == "__main__":
    main()
