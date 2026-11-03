"""将内部文本/工具消息与 Anthropic Messages 协议互相转换。"""

import json
from copy import deepcopy

from anthropic import Anthropic

from .config import anthropic_request_options
from .anthropic_profile import state_identity
from .model_content import validate_anthropic_state, validate_thinking
from .structured_output import StructuredResponseError


def _object_arguments(raw):
    def invalid(value):
        raise ValueError(f"工具参数含非法 JSON 数值：{value}")
    value = json.loads(raw, parse_constant=invalid)
    if not isinstance(value, dict):
        raise ValueError("工具参数必须是 JSON 对象。")
    return value


def prepare_messages(messages, definitions, system_prompt, *, thinking=None, model=None, profile=None):
    """生成请求副本；只将前置 system 放到顶层，按批合并 tool_result。"""
    system = [system_prompt] if definitions else []
    converted, pending = [], set()
    current_turn = max((i for i, item in enumerate(messages) if item.get("role") == "user"), default=-1)
    continuing_tools = messages and messages[-1].get("role") == "tool"
    for position, message in enumerate(messages):
        original_blocks = validate_anthropic_state(message)
        if original_blocks is not None and continuing_tools and position > current_turn:
            state = message["anthropic"]
            if ((thinking is not None and state["thinking"] != thinking) or (model is not None and state["model"] != model)
                    or state.get("provider") != (profile or {}).get("provider")):
                raise ValueError("同一工具任务内不能切换 Anthropic 服务、模型或思考配置。")
        if original_blocks is not None:
            state = message["anthropic"]
            # 新任务切换服务/型号/模式时只构造标准块，原历史继续保留完整内容。
            if (state.get("provider") != (profile or {}).get("provider") or (model is not None and state["model"] != model)
                    or (thinking is not None and state["thinking"] != thinking)):
                original_blocks = None
        role, content = message.get("role"), message.get("content")
        if role == "system":
            if converted or not isinstance(content, str) or not content.strip():
                raise ValueError("Anthropic 适配仅支持前置的非空 system 文本。")
            system.append(content)
            continue
        if role == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in pending or not isinstance(content, str):
                raise ValueError("工具结果缺少对应请求、重复或正文不是文本。")
            pending.remove(call_id)
            block = {"type": "tool_result", "tool_use_id": call_id, "content": content}
            if converted and converted[-1]["role"] == "user" and isinstance(converted[-1]["content"], list):
                converted[-1]["content"].append(block)
            else:
                converted.append({"role": "user", "content": [block]})
            continue
        if pending:
            raise ValueError("工具结果尚未完整回填。")
        if role not in {"user", "assistant"} or (content is not None and not isinstance(content, str)):
            raise ValueError("Anthropic 适配仅支持文本和已配对的工具消息。")
        blocks = [{"type": "text", "text": content}] if content else []
        calls = message.get("tool_calls", [])
        if not isinstance(calls, list) or (calls and role != "assistant"):
            raise ValueError("tool_calls 必须是 assistant 的调用列表。")
        for call in calls:
            function = call.get("function", {})
            call_id = call.get("id")
            if (call.get("type") != "function" or not isinstance(call_id, str) or not call_id.strip()
                    or call_id in pending or not isinstance(function, dict)
                    or not isinstance(function.get("name"), str) or not function["name"].strip()
                    or not isinstance(function.get("arguments"), str)):
                raise ValueError("工具请求格式无效或 ID 重复。")
            pending.add(call_id)
            blocks.append({"type": "tool_use", "id": call_id, "name": function["name"],
                           "input": _object_arguments(function["arguments"])})
        if not blocks:
            raise ValueError("消息缺少正文或工具请求。")
        if original_blocks is not None:
            blocks = original_blocks
        converted.append({"role": role, "content": blocks})
    if pending:
        raise ValueError("工具结果尚未完整回填。")
    if not converted or converted[0]["role"] != "user":
        raise ValueError("Anthropic 对话须从 user 消息开始。")
    options = {"messages": converted}
    if system:
        options["system"] = "\n\n".join(system)
    if definitions:
        options["tools"] = [{"name": tool["name"], "description": tool["description"],
                             "input_schema": tool["input_schema"]} for tool in definitions]
        options["tool_choice"] = {"type": "auto"}
    return options


def normalize_usage(usage):
    """包含缓存读写的输入 token；缺失主字段时不伪造完整用量。"""
    if not isinstance(usage, dict):
        return None
    keys = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    # SDK 会把未提供的可选缓存计数展开为 None；这与 input/output 主字段缺失不同。
    values = [0 if key.startswith("cache_") and usage.get(key) is None else usage.get(key) for key in keys]
    if any(type(value) is not int or value < 0 for value in values):
        return None
    incoming, outgoing, created, cached = values
    return {"prompt_tokens": incoming + created + cached, "completion_tokens": outgoing,
            "total_tokens": incoming + created + cached + outgoing,
            "cache_creation_input_tokens": created, "cache_read_input_tokens": cached}


def parse_message(message, *, tools_enabled, thinking=None, model=None, profile=None):
    """完整校验一次响应后返回内部 assistant 消息；不执行工具。"""
    thinking = validate_thinking(thinking or {"type": "disabled"})
    reason = message.get("stop_reason")
    if reason == "max_tokens":
        raise RuntimeError("模型回复达到输出上限，本次回复可能不完整，工具未执行。")
    if reason not in {"end_turn", "stop_sequence", "tool_use"} or message.get("role") != "assistant":
        raise RuntimeError(f"Anthropic 回复未正常完成（{reason}）。")
    blocks = message.get("content")
    if not isinstance(blocks, list):
        raise RuntimeError("Anthropic 回复缺少内容块。")
    parts, calls, ids = [], [], set()
    for block in blocks:
        if not isinstance(block, dict):
            raise RuntimeError("Anthropic 内容块格式无效。")
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif block.get("type") == "tool_use":
            call_id, name, arguments = block.get("id"), block.get("name"), block.get("input")
            if (not tools_enabled or not isinstance(call_id, str) or not call_id.strip() or call_id in ids
                    or not isinstance(name, str) or not name.strip() or not isinstance(arguments, dict)):
                raise RuntimeError("Anthropic 工具请求未启用、格式无效或 ID 重复，整批工具未执行。")
            ids.add(call_id)
            try:
                raw = json.dumps(arguments, ensure_ascii=False, allow_nan=False)
            except (ValueError, TypeError):
                raise RuntimeError("Anthropic 工具参数不是有效 JSON 对象。") from None
            calls.append({"id": call_id, "type": "function", "function": {"name": name, "arguments": raw}})
        elif block.get("type") in {"thinking", "redacted_thinking"} and thinking["type"] != "disabled":
            pass  # 完成标准消息后，一并校验思考签名与原始内容。
        else:
            raise RuntimeError("本步不支持该 Anthropic 内容块；仅支持 text/tool_use。")
    content = "".join(parts)
    if bool(calls) != (reason == "tool_use"):
        raise RuntimeError("Anthropic 工具请求与结束原因不一致，工具未执行。")
    if not calls and not content.strip():
        raise RuntimeError("模型没有返回有效的文本正文。")
    result = {"role": "assistant", "content": content or None}
    if calls:
        result["tool_calls"] = calls
    if thinking["type"] != "disabled":
        result["anthropic"] = {"model": model, "thinking": thinking, "content": deepcopy(blocks), **(profile or {})}
        try:
            validate_anthropic_state(result)
        except ValueError as error:
            raise RuntimeError(str(error)) from None
    return result


def get_anthropic_message(messages, definitions, config, *, system_prompt, on_usage, max_tokens, max_retries, response_schema=None):
    request = anthropic_request_options(config, default_max_tokens=1024 if definitions else 256, max_tokens=max_tokens)
    profile = state_identity(config)
    options = prepare_messages(messages, definitions, system_prompt, thinking=request["thinking"], model=config["model"], profile=profile)
    if response_schema is not None:
        options["output_config"] = {"format": {"type": "json_schema", "schema": response_schema}}
    with Anthropic(api_key=config["api_key"], base_url=config["base_url"], timeout=30.0,
                   max_retries=max_retries) as client:
        response = client.messages.create(model=config["model"], stream=False, **request, **options)
    document = response.model_dump(exclude_unset=True)
    if on_usage is not None:
        on_usage(normalize_usage(document.get("usage")))
    if response_schema is not None:
        content = "".join(item.get("text", "") for item in document.get("content", []) if item.get("type") == "text")
        reason = document.get("stop_reason")
        if reason == "refusal":
            raise StructuredResponseError("refusal", "provider_refusal", content)
        if reason == "max_tokens":
            raise StructuredResponseError("output_truncated", "max_tokens", content)
        if reason != "end_turn" or any(item.get("type") == "tool_use" for item in document.get("content", [])):
            raise StructuredResponseError("unexpected_response", "terminal_text_required", content)
    return parse_message(document, tools_enabled=bool(definitions), thinking=request["thinking"], model=config["model"], profile=profile)
