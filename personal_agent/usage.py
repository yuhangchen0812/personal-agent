"""累计当前运行实际收到的完整 token 用量，不推算缺失数据。"""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


UsageCallback = Callable[[dict | None], None]


def parse_token_price(value: str) -> Decimal:
    """解析单价或预算金额，拒绝负数、NaN 和无穷大。"""
    try:
        price = Decimal(value)
    except InvalidOperation:
        raise ValueError("金额必须是非负有限数字。") from None
    if not price.is_finite() or price < 0:
        raise ValueError("金额必须是非负有限数字。")
    return price


@dataclass(frozen=True)
class TokenPrices:
    input_per_million: Decimal
    output_per_million: Decimal

    def __post_init__(self) -> None:
        for price in (self.input_per_million, self.output_per_million):
            if not isinstance(price, Decimal) or not price.is_finite() or price < 0:
                raise ValueError("输入和输出单价必须是非负有限 Decimal，单位为元/百万 token。")


@dataclass
class UsageStats:
    calls: int = 0
    reported_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def record(self, usage: dict | None) -> None:
        """每次逻辑调用结束后记录一次；不完整或非法数据只记为缺失。"""
        self.calls += 1
        fields = ("prompt_tokens", "completion_tokens", "total_tokens")
        if not isinstance(usage, dict) or any(type(usage.get(key)) is not int or usage[key] < 0 for key in fields):
            return
        self.reported_calls += 1
        for key in fields:
            setattr(self, key, getattr(self, key) + usage[key])

    def format_summary(self) -> str:
        """返回终端显示文本；只有已收到的完整报告才计入合计。"""
        lines = [f"本次运行用量：逻辑调用 {self.calls} 次，完整用量 {self.reported_calls} 次，缺失 {self.calls - self.reported_calls} 次。"]
        if self.reported_calls:
            lines.append(f"已报告 token 合计：输入 {self.prompt_tokens}，输出 {self.completion_tokens}，总计 {self.total_tokens}。")
        else:
            lines.append("token 用量：暂无完整服务端统计。")
        lines.append("仅累计本次运行收到的完整用量；未报告的失败和重试用量不计入。")
        return "\n".join(lines)

    def estimate_cost(self, prices: TokenPrices | None) -> Decimal | None:
        """返回已报告用量的估算元金额；缺单价或完整用量时返回 None。"""
        if prices is None or not self.reported_calls:
            return None
        return (
            Decimal(self.prompt_tokens) * prices.input_per_million
            + Decimal(self.completion_tokens) * prices.output_per_million
        ) / Decimal(1_000_000)

    def check_budget(self, prices: TokenPrices | None, max_cost: Decimal | None) -> None:
        """预算可用时返回 None；达到预算或缺失用量时阻止后续调用。"""
        if max_cost is None:
            return
        if prices is None:
            raise ValueError("启用 --max-cost 必须同时配置输入和输出单价。")
        if self.calls != self.reported_calls:
            raise RuntimeError("已有模型调用缺少完整用量，无法确认剩余预算；本次运行不再发起模型请求或执行后续工具。")
        # 只有从未发出过逻辑调用时，才把初始费用明确视为 0。
        cost = self.estimate_cost(prices) if self.calls else Decimal(0)
        if cost >= max_cost:
            raise RuntimeError(f"已达到本次运行估算费用预算（已报告费用 {cost} 元，预算 {max_cost} 元），后续请求和工具未执行。")

    def format_cost_summary(self, prices: TokenPrices | None, max_cost: Decimal | None = None) -> str:
        """用量与估算费用一起显示；缺失信息明确标记。"""
        lines = [self.format_summary()]
        if max_cost is not None:
            lines.append(f"本次运行估算预算：{max_cost} 元。")
        if prices is None:
            lines.append("估算费用：未知；请同时设置 --input-price 和 --output-price（元/百万 token）。")
        else:
            lines.append(f"配置单价（元/百万 token）：输入 {prices.input_per_million}，输出 {prices.output_per_million}。")
            cost = self.estimate_cost(prices)
            if cost is None:
                lines.append("估算费用：未知（暂无完整服务端用量）。")
            else:
                lines.append(f"已报告用量估算费用：{cost:.6f} 元。")
            lines.append("按配置单价估算，仅覆盖本次运行的已报告完整用量，不代表实际账单。")
        return "\n".join(lines)
