"""通过选定协议取得统一的文本回复或工具请求。"""

import json
import sys
from collections.abc import Callable

from openai import OpenAI

from .anthropic_adapter import get_anthropic_message
from .anthropic_stream import stream_anthropic_message
from .config import load_model_config
from .model_errors import MODEL_API_ERRORS
from .model_content import without_provider_state
from .usage import UsageCallback
from .structured_output import StructuredResponseError, checked_schema
from .qwen_thinking import attach_state as attach_qwen_state, prepare_history as prepare_qwen_history, request_options as qwen_request_options


MAX_REQUEST_RETRIES = 1
AGENT_SYSTEM_PROMPT = (
    "你是 Personal Agent，用中文简洁回答。"
    "涉及本地文件时，先调用只读工具取得实际内容，不猜测路径或正文。"
    "工具路径相对于 Personal Agent 项目根目录；可先列出或搜索，再读取相关行。"
    "根据工具结果继续完成任务；工具失败时如实说明。"
    "若工具中提供 agent，可用 type=explore 只读调查、type=plan 获取结构化计划，或 type=general 在当前权限下完成局部任务；prompt 须写清目标和必要背景。"
    "agent 的类型候选若包含自定义角色，可按其精确 name 委派，来源按本轮元信息；角色方法只在子任务中加载，仍受声明工具与父级权限约束。"
    "委派不代表写入或 Shell 已获批准，各操作仍以实际权限与审批结果为准。"
    "子任务返回的计划是建议，不代表获准执行；继续遵守当前权限及 Plan 审批。"
    "相关技能候选直接匹配当前任务时，可调用 skill：inline 加载方法再继续，fork 由独立子任务返回结论；先查看实际返回，不把加载方法说成任务已完成。技能不批准后续操作，子任务不能递归 fork。"
    "需要先制定计划时，调用 enter_plan_mode，调查后将计划写入指定文件，再调用 exit_plan_mode 等待用户审批。"
    "以本次提供的 Plan 状态和工具审批结果为准；未批准不能执行计划。批准后具体操作仍遵守原权限规则。"
    "若提供 tool_search，调用尚未提供定义的外部 MCP 工具前先按名称或关键词检索；命中工具从下一次请求提供参数 Schema，再按精确名称调用。检索加载不代表操作已获批准。"
)


def _prepare_request(messages: list[dict], definitions: list[dict] | None, config=None) -> tuple[list[dict], dict]:
    """共用流式/非流式请求的系统指令与工具格式转换。"""
    options = {}
    request_messages = without_provider_state(messages)
    if config is not None:
        request_messages = prepare_qwen_history(messages, request_messages, config)
    if definitions:
        options["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["input_schema"],
                },
            }
            for tool in definitions
        ]
        options["tool_choice"] = "auto"
        request_messages = [{"role": "system", "content": AGENT_SYSTEM_PROMPT}, *request_messages]
    return request_messages, options


def get_model_message(
    messages: list[dict], definitions: list[dict] | None = None,
    *, on_usage: UsageCallback | None = None, max_tokens: int | None = None,
    response_schema: dict | None = None,
) -> dict:
    """返回 assistant 消息，保留正文和工具调用，供 Agent Loop 使用。"""
    if not messages:
        raise ValueError("messages 不能为空。")
    if max_tokens is not None and (type(max_tokens) is not int or max_tokens < 1):
        raise ValueError("max_tokens 必须是正整数。")
    schema = checked_schema(response_schema, definitions)
    config = load_model_config()
    if config.get("protocol", "openai") == "anthropic":
        return get_anthropic_message(
            messages, definitions, config, system_prompt=AGENT_SYSTEM_PROMPT, on_usage=on_usage,
            max_tokens=max_tokens,
            max_retries=MAX_REQUEST_RETRIES,
            response_schema=schema,
        )
    request_messages, options = _prepare_request(messages, definitions, config)
    options.update(qwen_request_options(config, default_max_tokens=1024 if definitions else 256, max_tokens=max_tokens))
    if schema is not None:
        options["response_format"] = {"type": "json_schema", "json_schema": {"name": "structured_response", "strict": True, "schema": schema}}
    with OpenAI(
        api_key=config["api_key"],
        base_url=config["base_url"],
        timeout=30.0,
        max_retries=MAX_REQUEST_RETRIES,
    ) as client:
        completion = client.chat.completions.create(
            model=config["model"],
            messages=request_messages,
            stream=False,
            **options,
        )

    if on_usage is not None:
        on_usage(completion.usage.model_dump() if completion.usage is not None else None)
    if not completion.choices:
        if schema is not None:
            raise StructuredResponseError("unexpected_response", "missing_choice")
        raise RuntimeError("模型没有返回回复候选。")

    choice = completion.choices[0]
    if schema is not None:
        content = choice.message.content
        if getattr(choice.message, "refusal", None) or choice.finish_reason == "content_filter":
            error = StructuredResponseError("refusal", "provider_refusal", content)
            error.raw_message["refusal"] = getattr(choice.message, "refusal", None)
            raise error
        if choice.finish_reason == "length":
            raise StructuredResponseError("output_truncated", "max_tokens", content)
        if choice.message.tool_calls or choice.finish_reason != "stop":
            raise StructuredResponseError("unexpected_response", "terminal_text_required", content)
        if not isinstance(content, str) or not content.strip():
            raise StructuredResponseError("unexpected_response", "empty_terminal_text", content)
    if choice.finish_reason == "length":
        raise RuntimeError("模型回复达到输出上限，本次回复可能不完整。")

    message = choice.message
    result = {"role": message.role, "content": message.content}
    if message.tool_calls:
        result["tool_calls"] = []
        for call in message.tool_calls:
            if call.type != "function":
                raise RuntimeError("当前只支持 function 类型工具。")
            # 兼容服务可能附加 index 等响应字段；历史只保存标准请求字段。
            result["tool_calls"].append({
                "id": call.id, "type": "function",
                "function": {"name": call.function.name, "arguments": call.function.arguments},
            })
    elif not isinstance(message.content, str) or not message.content.strip():
        raise RuntimeError("模型没有返回有效的文本正文。")
    return attach_qwen_state(result, getattr(message, "reasoning_content", None), config)


def get_model_reply(messages: list[dict[str, str]], *, on_usage: UsageCallback | None = None) -> str:
    """保留原来的纯文本请求接口，供独立连接演示使用。"""
    content = get_model_message(messages, on_usage=on_usage)["content"]
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("模型没有返回有效的文本正文。")
    return content.strip()


def _merge_tool_delta(calls: dict[int, dict], delta) -> None:
    """按 index 累积一次工具增量；此处不解析参数、不执行工具。"""
    index = delta.index
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        raise RuntimeError("流式工具 index 必须是非负整数。")
    if delta.type is not None and delta.type != "function":
        raise RuntimeError("当前只支持 function 工具流。")
    call = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
    if delta.id is not None:
        if not isinstance(delta.id, str):
            raise RuntimeError("流式工具 ID 片段必须是字符串。")
        call["id"] += delta.id
    if delta.function is not None:
        for field in ("name", "arguments"):
            fragment = getattr(delta.function, field)
            if fragment is not None:
                if not isinstance(fragment, str):
                    raise RuntimeError("流式工具名称和参数片段必须是字符串。")
                call["function"][field] += fragment


def stream_model_message(
    messages: list[dict], definitions: list[dict] | None = None,
    on_text: Callable[[str], None] | None = None,
    *, on_usage: UsageCallback | None = None,
) -> dict:
    """流式显示正文并拼接工具，整条响应完整后返回 assistant 字典。"""
    if not messages:
        raise ValueError("messages 不能为空。")
    config = load_model_config()
    if config.get("protocol", "openai") == "anthropic":
        return stream_anthropic_message(
            messages, definitions, config, system_prompt=AGENT_SYSTEM_PROMPT,
            on_text=on_text, on_usage=on_usage, max_tokens=None,
            max_retries=MAX_REQUEST_RETRIES,
        )
    request_messages, options = _prepare_request(messages, definitions, config)
    options.update(qwen_request_options(config, default_max_tokens=1024 if definitions else 256))
    parts = []
    reasoning_parts = []
    visible_started = False
    calls: dict[int, dict] = {}
    finished = None
    with OpenAI(
        api_key=config["api_key"], base_url=config["base_url"],
        timeout=30.0, max_retries=MAX_REQUEST_RETRIES,
    ) as client:
        with client.chat.completions.create(
            model=config["model"], messages=request_messages, stream=True,
            stream_options={"include_usage": True},
            **options,
        ) as chunks:
            for chunk in chunks:
                if chunk.usage is not None and on_usage is not None:
                    on_usage(chunk.usage.model_dump())
                if not chunk.choices:
                    continue
                if finished is not None:
                    raise RuntimeError("流式结束标记后又收到内容，响应无效。")
                choice = chunk.choices[0]
                reasoning = getattr(choice.delta, "reasoning_content", None)
                if config.get("thinking", {}).get("type") == "enabled" and reasoning is not None:
                    if not isinstance(reasoning, str) or reasoning and visible_started:
                        raise RuntimeError("Qwen 思考流片段类型或顺序无效，工具未执行。")
                    reasoning_parts.append(reasoning)
                if choice.delta.content or choice.delta.tool_calls:
                    visible_started = True
                if choice.delta.function_call:
                    raise RuntimeError("不支持旧式 function_call 流。")
                if choice.delta.tool_calls and not definitions:
                    raise RuntimeError("当前流式入口未启用工具，不执行工具请求。")
                for delta in choice.delta.tool_calls or []:
                    _merge_tool_delta(calls, delta)
                text = choice.delta.content
                if text is not None:
                    if not isinstance(text, str):
                        raise RuntimeError("流式正文片段不是字符串。")
                    if text:
                        parts.append(text)
                        if on_text is not None:
                            on_text(text)
                reason = choice.finish_reason
                if reason is not None:
                    finished = reason
    if finished is None:
        raise RuntimeError("流式连接已结束，但未收到完整回复的结束标记。")
    if finished == "length":
        raise RuntimeError("模型回复达到输出上限，流式回复未完整保存。")
    if finished not in {"stop", "tool_calls"}:
        raise RuntimeError(f"流式回复未正常完成（{finished}）。")
    content = "".join(parts)
    if calls:
        if finished != "tool_calls":
            raise RuntimeError("流式工具请求缺少 tool_calls 结束标记。")
        ordered = [calls[index] for index in sorted(calls)]
        ids = [call["id"] for call in ordered]
        if any(not value.strip() for value in ids) or len(set(ids)) != len(ids):
            raise RuntimeError("流式工具调用 ID 必须非空且不重复。")
        for call in ordered:
            if not call["function"]["name"].strip():
                raise RuntimeError("流式工具请求缺少工具名称。")
            try:
                arguments = json.loads(call["function"]["arguments"])
            except ValueError:
                raise RuntimeError("流式工具参数不是完整 JSON 对象，工具未执行。") from None
            if not isinstance(arguments, dict):
                raise RuntimeError("流式工具参数必须是 JSON 对象，工具未执行。")
        return attach_qwen_state({"role": "assistant", "content": content or None, "tool_calls": ordered}, "".join(reasoning_parts), config)
    if finished != "stop":
        raise RuntimeError("流式工具响应没有返回工具请求。")
    if not content.strip():
        raise RuntimeError("模型没有返回有效的文本正文。")
    return attach_qwen_state({"role": "assistant", "content": content}, "".join(reasoning_parts), config)


def stream_model_reply(
    messages: list[dict], on_text: Callable[[str], None],
    *, on_usage: UsageCallback | None = None,
) -> str:
    """保留上一小步的纯文本接口，返回完整正文。"""
    return stream_model_message(messages, on_text=on_text, on_usage=on_usage)["content"]


def main() -> None:
    """运行一次简短的模型连接演示。"""
    messages = [
        {"role": "user", "content": "请只回复：Personal Agent 已成功连接千问。"}
    ]
    print("正在发送一次非流式请求……")
    try:
        reply = get_model_reply(messages)
    except MODEL_API_ERRORS as error:
        status = getattr(error, "status_code", None)
        print(
            f"模型请求失败（{type(error).__name__}，状态码：{status}）。",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except (ValueError, RuntimeError) as error:
        print(f"请求失败：{error}", file=sys.stderr)
        raise SystemExit(1) from None

    print(f"千问回复：{reply}")


if __name__ == "__main__":
    main()
