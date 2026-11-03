"""文件应用共用的请求用量、预算参数和原子 JSON 报告写入。"""

from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile

from .llm import get_model_message


def plain_path(path):
    path = Path(os.path.abspath(path))
    if path.resolve() != path:
        raise ValueError("输入/输出路径不能通过链接重定向。")
    return path


def validate_budget(prices, max_cost):
    if max_cost is not None and (not isinstance(max_cost, Decimal) or not max_cost.is_finite() or max_cost < 0 or prices is None):
        raise ValueError("预算须为非负 Decimal，并配置两种单价。")


def usage_snapshot(stats, prices, max_cost):
    cost = stats.estimate_cost(prices)
    return {**vars(stats), "missing_calls": stats.calls - stats.reported_calls,
        "estimated_cost_yuan": str(cost) if cost is not None and stats.calls == stats.reported_calls else None,
        "max_cost_yuan": str(max_cost) if max_cost is not None else None}


def tracked_model(stats, settings, call_model=None):
    def model(messages, definitions=None, observe=None, *, response_schema=None):
        usage = None
        def capture(value):
            nonlocal usage
            usage = value
        try:
            options = {"response_schema": response_schema} if response_schema is not None else {}
            return (call_model or get_model_message)(messages, definitions, on_usage=capture, max_tokens=settings.get("max_tokens", 2048), **options)
        finally:
            stats.record(usage)
            if observe is not None:
                observe(usage)
    return model


def write_json_atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False) as target:
            temporary = Path(target.name)
            target.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
