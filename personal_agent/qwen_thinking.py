"""已核对 Qwen 3.7 配置的有限支持；不把兼容协议当成通用思考能力。"""

from copy import deepcopy
from urllib.parse import urlsplit


MODELS = {"qwen3.7-plus", "qwen3.7-plus-2026-05-26", "qwen3.7-flash", "qwen3.7-flash-2026-07-15"}
HOSTS = {"cn-beijing.maas.qianwenaiapi.com", "maas.qianwenaiapi.com"}


def _thinking(value):
    if (not isinstance(value, dict) or set(value) != {"type", "budget_tokens"} or value["type"] != "enabled"
            or type(value["budget_tokens"]) is not int or not 1 <= value["budget_tokens"] <= 32768):
        raise ValueError("Qwen enabled 思考预算须为 1～32768 的整数。")
    return deepcopy(value)


def request_options(config, *, default_max_tokens, max_tokens=None):
    maximum = max_tokens if max_tokens is not None else config.get("max_tokens", default_max_tokens)
    if type(maximum) is not int or maximum < 1:
        raise ValueError("输出上限必须为正整数。")
    thinking = config.get("thinking", {"type": "disabled"})
    if thinking == {"type": "disabled"}:
        return {"extra_body": {"enable_thinking": False}, "max_tokens": maximum}
    thinking = _thinking(thinking)
    parsed = urlsplit(config["base_url"])
    if (config["model"] not in MODELS or parsed.scheme != "https" or parsed.hostname not in HOSTS
            or parsed.path.rstrip("/") != "/compatible-mode/v1" or parsed.port not in {None, 443}
            or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment):
        raise ValueError("思考模式目前仅支持已核对的 Qwen 3.7 Plus/Flash 型号和千问官方端点，不推断其他兼容服务能力。")
    if thinking["budget_tokens"] >= maximum:
        raise ValueError("Qwen 思考预算须小于本次总输出上限。")
    return {"extra_body": {"enable_thinking": True, "thinking_budget": thinking["budget_tokens"], "preserve_thinking": True},
        "max_completion_tokens": maximum}


def validate_state(message):
    if "qwen" not in message:
        return None
    value = message["qwen"]
    if ("anthropic" in message or message.get("role") != "assistant" or not isinstance(value, dict)
            or set(value) != {"model", "thinking", "reasoning_content"} or not isinstance(value.get("model"), str) or value["model"] not in MODELS
            or not isinstance(value.get("reasoning_content"), str) or not value["reasoning_content"]
            or len(value["reasoning_content"]) > 2 * 1024 * 1024):
        raise ValueError("Qwen 原始思考扩展结构无效。")
    _thinking(value["thinking"])
    return deepcopy(value)


def attach_state(message, reasoning, config):
    if config.get("thinking", {}).get("type") != "enabled" or reasoning is None:
        return message
    if not isinstance(reasoning, str):
        raise RuntimeError("Qwen reasoning_content 必须是文本。")
    if reasoning:
        message["qwen"] = {"model": config["model"], "thinking": deepcopy(config["thinking"]), "reasoning_content": reasoning}
        validate_state(message)
    return message


def prepare_history(original, standard, config):
    """只给匹配的 Qwen 请求补回原字段；新任务可关闭/换模型，同一工具任务不能切换。"""
    thinking = config.get("thinking", {"type": "disabled"})
    current = max((i for i, item in enumerate(original) if item.get("role") == "user"), default=-1)
    continuing = original[-1].get("role") == "tool"
    for i, (source, target) in enumerate(zip(original, standard)):
        state = validate_state(source)
        if state is None:
            continue
        same_model = state["model"] == config["model"]
        if continuing and i > current and (not same_model or state["thinking"] != thinking):
            raise ValueError("同一工具任务内不能切换 Qwen 模型或思考配置。")
        if same_model and thinking.get("type") == "enabled":
            target["reasoning_content"] = state["reasoning_content"]
    return standard
