"""可注入模型函数的最小 Agent Loop，以及离线脚本演示。"""

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path
from threading import Event

from .cancellation import cancellation_scope, check_cancelled
from .permissions import READ_TOOLS
from .tool_output import prepare_tool_result, shrink_old_tool_results
from .tool_registry import TOOL_ERROR_PREFIX, execute_tool, get_tool_definitions


ModelCall = Callable[[list[dict], list[dict]], dict]
ToolExecutor = Callable[[str, dict], str]


def _execute_tool_call(tool_call: dict, executor: ToolExecutor | None = None) -> str:
    """解析一次工具请求；普通执行错误转成结果，保持调用 ID 配对。"""
    if tool_call.get("type") != "function":
        return f"{TOOL_ERROR_PREFIX}当前只支持 function 类型工具。"
    function = tool_call.get("function")
    if not isinstance(function, dict):
        return f"{TOOL_ERROR_PREFIX}缺少 function 对象。"
    try:
        arguments = json.loads(function.get("arguments", ""))
    except (ValueError, TypeError):
        return f"{TOOL_ERROR_PREFIX}arguments 不是有效的 JSON 字符串。"
    try:
        dispatch = executor if executor is not None else execute_tool
        return dispatch(function.get("name"), arguments)
    except Exception as error:
        # 在工具执行边界返回错误结果，让同批其他调用仍能完成配对。
        return f"{TOOL_ERROR_PREFIX}工具执行异常（{type(error).__name__}）。"


def _is_read_call(tool_call: dict) -> bool:
    """仅已知的 function 只读调用可进入并发组；其余调用形成顺序屏障。"""
    function = tool_call.get("function")
    return (
        tool_call.get("type") == "function"
        and isinstance(function, dict)
        and isinstance(function.get("name"), str)
        and function["name"] in READ_TOOLS
    )


def _execute_read_call(tool_call: dict, executor: ToolExecutor | None, event: Event) -> str:
    """给本组工作线程绑定同一个取消事件，开始执行前先检查一次。"""
    with cancellation_scope(event):
        check_cancelled()
        return _execute_tool_call(tool_call, executor)


def _execute_tool_batch(
    tool_calls: list[dict], executor: ToolExecutor | None, max_parallel_reads: int,
) -> list[str]:
    """连续只读调用有限并发，其他调用在主线程顺序执行；按请求顺序返回结果。"""
    results = []
    start = 0
    while start < len(tool_calls):
        end = start + 1
        if _is_read_call(tool_calls[start]):
            while end < len(tool_calls) and _is_read_call(tool_calls[end]):
                end += 1
        group = tool_calls[start:end]
        if len(group) == 1 or max_parallel_reads == 1:
            results.extend(_execute_tool_call(call, executor) for call in group)
        else:
            event = Event()
            with ThreadPoolExecutor(max_workers=min(max_parallel_reads, len(group))) as pool:
                futures = []
                try:
                    # Each worker inherits the caller's workspace and request scope in its own Context.
                    futures = [pool.submit(copy_context().run, _execute_read_call, call, executor, event) for call in group]
                    # result() 按提交顺序读取；完成快的工具不会抢占别人的调用 ID。
                    for future in futures:
                        while True:
                            try:
                                results.append(future.result(timeout=0.1))
                                break
                            except TimeoutError:
                                # 定期回到 Python，及时处理主线程的 Ctrl+C。
                                continue
                except BaseException:
                    event.set()  # 已运行任务在下一个检查点退出。
                    for future in futures:
                        future.cancel()
                    raise
        start = end
    return results


def run_agent_loop(
    messages: list[dict], call_model: ModelCall, max_turns: int = 5,
    tool_executor: ToolExecutor | None = None,
    *, max_parallel_reads: int = 3,
) -> str:
    """扩展历史并返回正文；限制模型次数和连续只读工具并发数（1 表示串行）。"""
    if not messages:
        raise ValueError("messages 不能为空。")
    if not isinstance(max_turns, int) or isinstance(max_turns, bool) or max_turns < 1:
        raise ValueError("max_turns 必须是正整数。")
    if not isinstance(max_parallel_reads, int) or isinstance(max_parallel_reads, bool) or max_parallel_reads < 1:
        raise ValueError("max_parallel_reads 必须是正整数。")

    shrink_old_tool_results(messages)
    definitions = get_tool_definitions()
    for turn in range(max_turns):
        message = call_model(messages, definitions)
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ValueError("模型调用函数必须返回 assistant 消息字典。")
        tool_calls = message.get("tool_calls", [])
        if not isinstance(tool_calls, list):
            raise ValueError("tool_calls 必须是列表。")
        ids = [call.get("id") if isinstance(call, dict) else None for call in tool_calls]
        if any(not isinstance(call_id, str) or not call_id.strip() for call_id in ids) or len(set(ids)) != len(ids):
            raise ValueError("同一消息中的工具调用 ID 必须非空且不重复。")

        if not tool_calls:
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("模型没有返回工具请求或有效正文。")
            messages.append(message)
            return content

        messages.append(message)
        if turn == max_turns - 1:
            results = [f"{TOOL_ERROR_PREFIX}已达到模型请求次数上限，工具未执行。"] * len(tool_calls)
        else:
            results = _execute_tool_batch(tool_calls, tool_executor, max_parallel_reads)
        for tool_call, result in zip(tool_calls, results):
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "content": prepare_tool_result(result),
            })
    raise RuntimeError(f"已达到模型请求次数上限（{max_turns} 次），尚未得到最终回答。")


def main() -> None:
    """固定响应模拟模型；文件工具真实执行，不请求模型服务。"""
    scenario_path = Path(__file__).resolve().parent.parent / "examples/agent-loop-responses.json"
    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
    responses = iter(scenario["responses"])
    request_count = 0

    def scripted_model(messages: list[dict], definitions: list[dict]) -> dict:
        nonlocal request_count
        request_count += 1
        print(f"[模拟请求 {request_count}] 发送 {len(messages)} 条消息、{len(definitions)} 个工具说明")
        try:
            return next(responses)
        except StopIteration:
            raise RuntimeError("固定模拟响应已用完。") from None

    print("离线演示：模型响应和最终解释均预设，文件工具真实执行。")
    print(f"用户：{scenario['user_message']}")
    messages = [{"role": "user", "content": scenario["user_message"]}]
    run_agent_loop(messages, scripted_model)
    for message in messages[1:]:
        if message.get("tool_calls"):
            for tool_call in message["tool_calls"]:
                print(f"\n工具请求 {tool_call['id']}：{tool_call['function']['name']}")
        elif message["role"] == "tool":
            print(f"工具结果 {message['tool_call_id']}：\n{message['content']}")
        else:
            print(f"\n模拟模型最终回答：{message['content']}")


if __name__ == "__main__":
    main()
