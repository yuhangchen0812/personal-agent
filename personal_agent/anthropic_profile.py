"""显式区分原生 Messages 与千问兼容网关；不按域名猜测凭据或签名能力。"""

from urllib.parse import urlsplit

from .qwen_thinking import MODELS


QWEN_BASE_URL = "https://maas.qianwenaiapi.com/apps/anthropic"


def validate_profile(config):
    """返回 native/qwen；qwen 仅接受已核对的有限型号与官方 HTTPS 端点。"""
    provider = config.get("anthropic_provider", "native")
    if provider not in {"native", "qwen"}:
        raise ValueError("anthropic_provider 只能为 native 或 qwen。")
    if provider == "qwen":
        parsed = urlsplit(config.get("base_url", ""))
        try:
            valid = (parsed.scheme == "https" and parsed.hostname == "maas.qianwenaiapi.com"
                     and parsed.port in {None, 443} and parsed.path.rstrip("/") == "/apps/anthropic"
                     and parsed.username is None and parsed.password is None and not parsed.query and not parsed.fragment
                     and config.get("model") in MODELS)
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("qwen Messages 配置仅支持已核对的 Qwen 3.7 型号与官方 /apps/anthropic 端点。")
        thinking = config.get("thinking", {"type": "disabled"})
        if thinking.get("type") not in {"disabled", "enabled"}:
            raise ValueError("qwen Messages 仅支持显式 disabled/enabled，不支持 adaptive。")
        if thinking.get("type") == "enabled" and not 1024 <= thinking.get("budget_tokens", 0) <= 32768:
            raise ValueError("qwen Messages 项目思考预算须为 1024～32768。")
    return provider


def state_identity(config):
    """可保存的协议来源标记，不含凭据，也不证明远端身份。"""
    if validate_profile(config) == "qwen":
        return {"provider": "qwen", "base_url": QWEN_BASE_URL}
    return {}
