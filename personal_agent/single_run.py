"""单次任务状态与输出协议；任务执行复用聊天运行时。"""

from contextlib import redirect_stdout
from dataclasses import dataclass, field
from decimal import Decimal
import json
import sys

from .mcp_client import McpError
from .model_errors import MODEL_API_ERRORS
from .tool_registry import TOOL_ERROR_PREFIX
from .tools import (ERROR_PREFIX, LIST_ERROR_PREFIX, SEARCH_ERROR_PREFIX, WRITE_ERROR_PREFIX,
                    EDIT_ERROR_PREFIX, SHELL_ERROR_PREFIX)
from .usage import TokenPrices, UsageStats


ERROR_PREFIXES = (TOOL_ERROR_PREFIX, ERROR_PREFIX, LIST_ERROR_PREFIX, SEARCH_ERROR_PREFIX,
                  WRITE_ERROR_PREFIX, EDIT_ERROR_PREFIX, SHELL_ERROR_PREFIX)


@dataclass
class SingleRun:
    prompt: str
    consumed: bool = False
    status: str = "failed"
    answer: str | None = None
    error: dict | None = None
    model: dict | None = None
    usage: UsageStats | None = None
    prices: TokenPrices | None = None
    max_cost: Decimal | None = None
    session: dict | None = None
    trace: list = field(default_factory=list)

    def bind(self, settings, usage, prices, max_cost, session, resume):
        self.model = {key: settings[key] for key in ("protocol", "model", "base_url", "thinking", "max_tokens") if key in settings}
        self.usage, self.prices, self.max_cost = usage, prices, max_cost
        if session is not None:
            self.session = {"name": session.name, "path": str(session.path), "resumed": resume, "saved": None}

    def capture(self, messages):
        """保存本轮已有调用/结果摘要；结果缺失不等于确定没有发生副作用。"""
        trace, pending = [], {}
        for message in messages:
            for call in message.get("tool_calls", []):
                function = call.get("function")
                item = {"id": call["id"], "name": function.get("name") if isinstance(function, dict) else None,
                        "outcome": "unknown", "result_preview": None, "result_truncated": False}
                trace.append(item)
                pending[call["id"]] = item
            if message.get("role") == "tool" and message.get("tool_call_id") in pending:
                item = pending.pop(message["tool_call_id"])
                result = message["content"]
                item.update(outcome="reported_error" if result.startswith(ERROR_PREFIXES) else "returned",
                            result_preview=result[:1000], result_truncated=len(result) > 1000)
        self.trace = trace

    def fail(self, error, *, phase="task", cancelled=False):
        detail = {"type": type(error).__name__, "phase": phase}
        if isinstance(error, MODEL_API_ERRORS):
            detail["status_code"] = getattr(error, "status_code", None)
        else:
            detail["message"] = "任务已取消。" if cancelled else str(error)
        if self.error is None or cancelled:
            self.error = detail
        self.status = "cancelled" if cancelled else "failed"

    def complete(self, answer, messages):
        self.answer = answer
        self.capture(messages)
        if self.error is None:
            self.status = "completed"

    def report(self):
        stats = self.usage
        usage = None
        if stats is not None:
            usage = {"calls": stats.calls, "reported_calls": stats.reported_calls,
                     "missing_calls": stats.calls - stats.reported_calls,
                     "complete": stats.calls == stats.reported_calls}
            known = stats.calls == 0 or stats.reported_calls > 0
            usage.update({key: getattr(stats, key) if known else None
                          for key in ("prompt_tokens", "completion_tokens", "total_tokens")})
        cost = stats.estimate_cost(self.prices) if stats is not None else None
        return {"version": 1, "status": self.status,
                "exit_code": 0 if self.status == "completed" else (130 if self.status == "cancelled" else 1),
                "answer": self.answer, "error": self.error, "model": self.model,
                "usage": usage, "estimated_cost_yuan": str(cost) if cost is not None else None,
                "max_cost_yuan": str(self.max_cost) if self.max_cost is not None else None,
                "session": self.session, "tool_trace": self.trace,
                "tool_error_count": sum(item["outcome"] == "reported_error" for item in self.trace)}


def run_once(prompt: str, **settings) -> dict:
    """执行一个普通任务；不模拟终端输入，不让审批读取 stdin。"""
    from .main import run_repl
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("单次任务 prompt 必须是非空文本。")
    task = SingleRun(prompt)
    with redirect_stdout(sys.stderr):
        try:
            run_repl(single_run=task, **settings)
        except KeyboardInterrupt as error:
            task.fail(error, cancelled=True)
        except MODEL_API_ERRORS as error:
            task.fail(error)
        except (ValueError, OSError, RuntimeError, McpError) as error:
            task.fail(error, phase="startup" if not task.consumed else "task")
    return task.report()


def print_report(report, output_format):
    if output_format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        if report["answer"] is not None:
            print(report["answer"])
        if report["error"] is not None:
            error = report["error"]
            print(f"单次任务失败：{error['type']}；{error.get('message', '模型请求失败。')}", file=sys.stderr)
