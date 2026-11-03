"""保留协议原始内容，同时校验它与应用内正文/工具调用一致。"""

import json
from copy import deepcopy
from .qwen_thinking import validate_state as validate_qwen_state
from .anthropic_profile import QWEN_BASE_URL, validate_profile


def validate_thinking(value):
    if not isinstance(value, dict) or not isinstance(value.get("type"), str) or value["type"] not in {"disabled", "adaptive", "enabled"}:
        raise ValueError("Anthropic thinking 须为 disabled、adaptive 或 enabled。")
    fields = {"type", "budget_tokens"} if value["type"] == "enabled" else {"type"}
    if set(value) != fields or (value["type"] == "enabled" and
            (type(value["budget_tokens"]) is not int or value["budget_tokens"] < 1024)):
        raise ValueError("仅 enabled 使用 budget_tokens，且须为至少 1024 的整数。")
    return deepcopy(value)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def validate_anthropic_state(message):
    """先校验供应商扩展，再返回 Anthropic 原始块；不验证签名的密码学有效性。"""
    validate_qwen_state(message)
    if "anthropic" not in message:
        return None
    state = message["anthropic"]
    gateway = isinstance(state, dict) and state.get("provider") == "qwen"
    fields = {"model", "thinking", "content"} | ({"provider", "base_url"} if gateway else set())
    if (message.get("role") != "assistant" or not isinstance(state, dict)
            or set(state) != fields
            or not isinstance(state["model"], str) or not state["model"].strip()
            or not isinstance(state["content"], list) or not state["content"]):
        raise ValueError("Anthropic 原始消息扩展结构无效。")
    thinking = validate_thinking(state["thinking"])
    if gateway:
        validate_profile({"anthropic_provider": "qwen", **state})
        if state["base_url"] != QWEN_BASE_URL:
            raise ValueError("qwen 原始状态须使用规范端点。")
    text, calls = [], []
    for block in state["content"]:
        if not isinstance(block, dict):
            raise ValueError("Anthropic 原始内容块无效。")
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            text.append(block["text"])
        elif kind == "tool_use" and isinstance(block.get("input"), dict):
            if any(not isinstance(block.get(key), str) or not block[key].strip() for key in ("id", "name")):
                raise ValueError("Anthropic 原始工具 ID/名称无效。")
            calls.append((block["id"], block["name"], _json(block["input"])))
        elif kind == "thinking" and thinking["type"] != "disabled":
            if not isinstance(block.get("thinking"), str) or not isinstance(block.get("signature"), str):
                raise ValueError("思考块须保留文本及 signature 字段。")
            if gateway and block["signature"] != "":
                raise ValueError("qwen 网关当前仅支持空 signature；不推断其签名能力。")
            if not gateway and not block["signature"]:
                raise ValueError("思考块须保留完整文本及非空签名。")
        elif kind == "redacted_thinking" and thinking["type"] != "disabled" and not gateway:
            if not isinstance(block.get("data"), str) or not block["data"]:
                raise ValueError("被遮蔽的思考块须保留非空 data。")
        else:
            raise ValueError("Anthropic 原始内容块类型未支持或与思考模式不符。")
    if "".join(text) != (message.get("content") or ""):
        raise ValueError("原始内容与 assistant 正文不一致。")
    try:
        standard = [(call["id"], call["function"]["name"], _json(json.loads(call["function"]["arguments"])))
                    for call in message.get("tool_calls", []) if call["type"] == "function"]
        if len(standard) != len(message.get("tool_calls", [])) or calls != standard or len({call[0] for call in calls}) != len(calls):
            raise ValueError("原始内容与 assistant 工具调用不一致。")
        _json(state)
    except (KeyError, TypeError):
        raise ValueError("Anthropic 工具调用或原始扩展不是合法 JSON。") from None
    return deepcopy(state["content"])


def without_provider_state(messages):
    """生成供其他协议或摘要使用的标准消息副本，原历史继续保留完整数据。"""
    result = []
    for message in messages:
        validate_anthropic_state(message)
        result.append(deepcopy({key: value for key, value in message.items() if key not in {"anthropic", "qwen"}}))
    return result
