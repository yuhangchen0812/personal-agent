"""读取 Personal Agent 使用的模型配置。"""

import os
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

from .model_content import validate_thinking
from .anthropic_profile import QWEN_BASE_URL, validate_profile


DEFAULT_BASE_URL = "https://cn-beijing.maas.qianwenaiapi.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen3.7-plus"
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
_RUN_SETTINGS = ContextVar("personal_agent_model_settings", default=None)
OVERRIDE_FIELDS = {"protocol", "model", "base_url", "thinking", "thinking_budget", "max_tokens", "anthropic_provider"}


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 必须是非空字符串。")
    return value.strip()


def _integer(value, label):
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError:
            raise ValueError(f"{label} 必须为正整数。") from None
    if type(value) is not int or value < 1:
        raise ValueError(f"{label} 必须为正整数。")
    return value


def _base_url(value):
    value = _text(value, "API 根地址")
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in {"http", "https"} and parsed.hostname and parsed.username is None
                 and parsed.password is None and not parsed.query and not parsed.fragment
                 and not any(char.isspace() for char in value))
        parsed.port  # 前置检查非法端口；错误中不回显完整地址。
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("API 根地址须为完整 HTTP(S) 地址，不含凭据、查询串、片段或空白。")
    return value


def _resolve_settings(overrides=None):
    """CLI 覆盖 > 进程环境/.env > 默认值；只解析选定协议的配置。"""
    overrides = {} if overrides is None else overrides
    if not isinstance(overrides, dict) or set(overrides) - OVERRIDE_FIELDS:
        raise ValueError("模型覆盖只支持协议、模型、根地址、思考、输出上限和 anthropic_provider，不接受密钥。")
    load_dotenv(ENV_FILE, override=False, encoding="utf-8")
    protocol = _text(overrides.get("protocol", os.getenv("PERSONAL_AGENT_PROTOCOL", "openai")), "protocol").lower()

    def chosen(key, environment, default=None):
        if key in overrides:
            return overrides[key]
        return os.getenv(environment, "").strip() or default
    if protocol == "openai":
        if "anthropic_provider" in overrides:
            raise ValueError("--anthropic-provider 仅用于 anthropic 协议。")
        settings = {
            "protocol": protocol,
            "base_url": _base_url(chosen("base_url", "PERSONAL_AGENT_BASE_URL", DEFAULT_BASE_URL)),
            "model": _text(chosen("model", "PERSONAL_AGENT_MODEL", DEFAULT_MODEL), "model"),
        }
        maximum = chosen("max_tokens", "PERSONAL_AGENT_MAX_TOKENS")
        if maximum is not None:
            settings["max_tokens"] = _integer(maximum, "max_tokens")
        mode = _text(chosen("thinking", "PERSONAL_AGENT_THINKING", "disabled"), "thinking").lower()
        budget = chosen("thinking_budget", "PERSONAL_AGENT_THINKING_BUDGET")
        if "thinking" in overrides and mode == "disabled" and "thinking_budget" not in overrides:
            budget = None
        if mode not in {"enabled", "disabled"} or mode == "disabled" and budget is not None:
            raise ValueError("OpenAI 兼容分支的 Qwen 配置仅支持 enabled/disabled；只有 enabled 可指定思考预算。")
        if mode == "enabled":
            settings["thinking"] = {"type": "enabled", "budget_tokens": _integer(budget if budget is not None else 1024, "thinking_budget")}
            settings.setdefault("max_tokens", 4096)
            from .qwen_thinking import request_options
            request_options(settings, default_max_tokens=4096)
        return settings
    if protocol == "anthropic":
        provider = _text(chosen("anthropic_provider", "ANTHROPIC_PROVIDER", "native"), "anthropic_provider").lower()
        model = chosen("model", "ANTHROPIC_MODEL", DEFAULT_MODEL if provider == "qwen" else None)
        if not model:
            raise ValueError("使用 anthropic 协议需要设置 ANTHROPIC_MODEL，填写服务实际支持的模型名。")
        settings = {
            "protocol": protocol,
            "base_url": _base_url(chosen("base_url", "ANTHROPIC_BASE_URL", QWEN_BASE_URL if provider == "qwen" else "https://api.anthropic.com")),
            "model": _text(model, "model"),
        }
        if provider != "native":
            settings["anthropic_provider"] = provider
        mode = _text(chosen("thinking", "ANTHROPIC_THINKING", "disabled"), "thinking").lower()
        thinking = {"type": mode}
        # 显式关闭/切换 adaptive 时清除旧模式的环境预算；显式预算仍须与模式相容。
        budget = chosen("thinking_budget", "ANTHROPIC_THINKING_BUDGET")
        if "thinking" in overrides and mode != "enabled" and "thinking_budget" not in overrides:
            budget = None
        if budget is not None:
            thinking["budget_tokens"] = _integer(budget, "ANTHROPIC_THINKING_BUDGET")
        settings["thinking"] = validate_thinking(thinking)
        maximum = chosen("max_tokens", "ANTHROPIC_MAX_TOKENS")
        if maximum is not None:
            settings["max_tokens"] = _integer(maximum, "ANTHROPIC_MAX_TOKENS")
        elif mode != "disabled":
            settings["max_tokens"] = 4096
        anthropic_request_options(settings, default_max_tokens=1024)
        return settings
    raise ValueError("PERSONAL_AGENT_PROTOCOL 只能为 openai 或 anthropic。")


def get_model_settings() -> dict:
    """作用域内返回固定非密钥快照的副本；无覆盖时保留原环境读取行为。"""
    snapshot = _RUN_SETTINGS.get()
    return deepcopy(snapshot) if snapshot is not None else _resolve_settings()


@contextmanager
def model_config_scope(overrides):
    """冻结本次配置，退出时恢复调用方作用域；不将覆盖写入环境变量。"""
    settings = _resolve_settings(overrides)
    token = _RUN_SETTINGS.set(settings)
    try:
        yield deepcopy(settings)
    finally:
        _RUN_SETTINGS.reset(token)


def anthropic_request_options(config, *, default_max_tokens, max_tokens=None):
    """显式调用上限优先，其次项目配置，最后文本/工具入口默认值。"""
    thinking = validate_thinking(config.get("thinking", {"type": "disabled"}))
    validate_profile({**config, "thinking": thinking})
    maximum = max_tokens if max_tokens is not None else config.get("max_tokens", default_max_tokens)
    if type(maximum) is not int or maximum < 1:
        raise ValueError("Anthropic max_tokens 必须为正整数。")
    if thinking["type"] == "enabled" and thinking["budget_tokens"] >= maximum:
        raise ValueError("思考 budget_tokens 必须小于 max_tokens 输出上限。")
    return {"thinking": thinking, "max_tokens": maximum}


def load_model_config() -> dict:
    """从环境变量或本项目 .env 读取密钥，返回模型配置。"""
    settings = get_model_settings()
    key_name = ("ANTHROPIC_API_KEY" if settings["protocol"] == "anthropic"
                and validate_profile(settings) == "native" else "DASHSCOPE_API_KEY")
    api_key = os.getenv(key_name, "").strip()

    if not api_key:
        raise ValueError(
            f"未找到 {key_name}，请在环境变量或项目根目录的 .env 中设置。"
        )

    return {
        "api_key": api_key,
        **settings,
    }
