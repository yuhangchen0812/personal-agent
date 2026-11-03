"""组装 Anthropic 原始流事件；整条消息完成后才返回可执行工具请求。"""

from anthropic import Anthropic, APIConnectionError
import httpx2

from .anthropic_adapter import _object_arguments, normalize_usage, parse_message, prepare_messages
from .config import anthropic_request_options
from .anthropic_profile import state_identity


class MessageStream:
    """一次 HTTP 响应的状态；不拥有权限、文件工具或聊天历史。"""

    def __init__(self, on_text=None, *, thinking=None, model=None, profile=None):
        self.on_text = on_text
        self.thinking = thinking or {"type": "disabled"}
        self.model = model
        self.profile = profile
        self.started = self.stopped = self.delta_phase = False
        self.blocks = {}
        self.open_blocks = set()
        self.usage = {}
        self.final_usage_received = False
        self.reason = None

    def _text(self, value):
        if not isinstance(value, str):
            raise RuntimeError("Anthropic 流式正文片段必须为字符串。")
        if value and self.on_text is not None:
            self.on_text(value)

    def feed(self, event):
        """接收一个原始事件，只更新状态及显示正文，不执行工具。"""
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise RuntimeError("Anthropic 流事件格式无效。")
        kind = event.get("type")
        if kind == "ping":
            return
        if self.stopped:
            raise RuntimeError("Anthropic message_stop 后又收到内容，整条响应无效。")
        if kind == "message_start":
            message = event.get("message", {})
            if self.started or not isinstance(message, dict) or message.get("role") != "assistant" or message.get("content") != []:
                raise RuntimeError("Anthropic message_start 格式或顺序无效。")
            self.started = True
            usage = message.get("usage")
            self.usage = dict(usage) if isinstance(usage, dict) else {}
            return
        if not self.started:
            raise RuntimeError("Anthropic 流缺少 message_start。")
        if kind in {"content_block_start", "content_block_delta", "content_block_stop"}:
            if self.delta_phase:
                raise RuntimeError("Anthropic message_delta 后不能继续内容块。")
            index = event.get("index")
            if type(index) is not int or index < 0:
                raise RuntimeError("Anthropic 内容块 index 必须为非负整数。")
            if kind == "content_block_start":
                block = event.get("content_block")
                if index in self.blocks or not isinstance(block, dict):
                    raise RuntimeError("Anthropic 内容块重复或格式无效。")
                if block.get("type") == "text":
                    self._text(block.get("text"))
                elif block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
                    pass
                elif block.get("type") == "thinking" and self.thinking["type"] != "disabled" and isinstance(block.get("thinking"), str):
                    if not isinstance(block.get("signature", ""), str):
                        raise RuntimeError("Anthropic 思考签名片段必须为字符串。")
                    block = {**block, "signature": block.get("signature", "")}
                elif block.get("type") == "redacted_thinking" and self.thinking["type"] != "disabled" and isinstance(block.get("data"), str):
                    pass
                else:
                    raise RuntimeError("Anthropic 内容块未支持，或思考块与当前模式不符。")
                self.blocks[index] = {**block}
                self.open_blocks.add(index)
                return
            if index not in self.open_blocks:
                raise RuntimeError("Anthropic 增量或结束事件没有对应的活动内容块。")
            if kind == "content_block_stop":
                self.open_blocks.remove(index)
                return
            block = self.blocks[index]
            delta = event.get("delta", {})
            if not isinstance(delta, dict):
                raise RuntimeError("Anthropic 内容增量格式无效。")
            if block["type"] == "text" and delta.get("type") == "text_delta":
                self._text(delta.get("text"))
                block["text"] += delta["text"]
            elif block["type"] == "tool_use" and delta.get("type") == "input_json_delta" and isinstance(delta.get("partial_json"), str):
                block["partial_json"] = block.get("partial_json", "") + delta["partial_json"]
            elif block["type"] == "thinking" and delta.get("type") == "thinking_delta" and isinstance(delta.get("thinking"), str):
                block["thinking"] += delta["thinking"]
            elif block["type"] == "thinking" and delta.get("type") == "signature_delta" and isinstance(delta.get("signature"), str):
                block["signature"] += delta["signature"]
            else:
                raise RuntimeError("Anthropic 增量类型与内容块不匹配或尚未支持。")
            return
        if kind == "message_delta":
            if self.open_blocks:
                raise RuntimeError("Anthropic 内容块尚未结束，不能结束消息。")
            self.delta_phase = True
            delta = event.get("delta", {})
            if not isinstance(delta, dict):
                raise RuntimeError("Anthropic 消息增量格式无效。")
            reason = delta.get("stop_reason")
            if reason is not None:
                if not isinstance(reason, str):
                    raise RuntimeError("Anthropic 结束原因格式无效。")
                if self.reason is not None and reason != self.reason:
                    raise RuntimeError("Anthropic 流出现冲突的结束原因。")
                self.reason = reason
            usage = event.get("usage")
            if isinstance(usage, dict):
                self.usage.update(usage)  # 计数为累计值；缺省字段不覆盖 message_start。
                if self.reason is not None and "output_tokens" in usage:
                    self.final_usage_received = True
            return
        if kind == "message_stop":
            if not self.delta_phase or self.reason is None or self.open_blocks:
                raise RuntimeError("Anthropic message_stop 前缺少完整内容或结束原因。")
            self.stopped = True
            return
        # 未知事件可能携带当前不能保真保存的内容，明确失败，避免悄悄丢弃。
        raise RuntimeError("当前尚未支持此 Anthropic 流事件。")

    def reported_usage(self):
        """只将带最终输出统计的累计用量交给预算；不把起始值冒充完成值。"""
        return normalize_usage(self.usage) if self.final_usage_received else None

    def finish(self, *, tools_enabled):
        if not self.stopped:
            raise RuntimeError("Anthropic 流已结束，但未收到完整消息的 message_stop 结束标记。")
        if sorted(self.blocks) != list(range(len(self.blocks))):
            raise RuntimeError("Anthropic 内容块 index 不连续。")
        blocks = []
        for index in sorted(self.blocks):
            block = dict(self.blocks[index])
            fragments = block.pop("partial_json", "")
            if fragments:
                if block.get("input") != {}:
                    raise RuntimeError("Anthropic 工具初始参数与流式 JSON 同时含值，不能无损合并。")
                try:
                    block["input"] = _object_arguments(fragments)
                except (ValueError, TypeError):
                    raise RuntimeError("Anthropic 流式工具参数不是完整 JSON 对象，整批工具未执行。") from None
            blocks.append(block)
        return parse_message({"role": "assistant", "content": blocks, "stop_reason": self.reason},
                             tools_enabled=tools_enabled, thinking=self.thinking, model=self.model, profile=self.profile)


def stream_anthropic_message(messages, definitions, config, *, system_prompt, on_text, on_usage, max_tokens, max_retries):
    request = anthropic_request_options(config, default_max_tokens=1024 if definitions else 256, max_tokens=max_tokens)
    profile = state_identity(config)
    options = prepare_messages(messages, definitions, system_prompt, thinking=request["thinking"], model=config["model"], profile=profile)
    state = MessageStream(on_text, thinking=request["thinking"], model=config["model"], profile=profile)
    try:
        with Anthropic(api_key=config["api_key"], base_url=config["base_url"], timeout=30.0,
                       max_retries=max_retries) as client:
            with client.messages.create(model=config["model"], stream=True, **request, **options) as stream:
                try:
                    for event in stream:
                        # 排除 SDK 自动补出的可选 None 字段，保留原事件提供的累计统计。
                        state.feed(event.model_dump(exclude_unset=True))
                except httpx2.HTTPError as error:
                    # 原始 SSE 读取错误不会全部被 SDK 包装；复用应用已有的脱敏回滚分支。
                    raise APIConnectionError(request=stream.response.request) from error
        return state.finish(tools_enabled=bool(definitions))
    finally:
        if on_usage is not None:
            on_usage(state.reported_usage())
