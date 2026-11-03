"""只读工作线程的协作取消；取消信号不放进模型参数。"""

from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event
from threading import current_thread, main_thread
from dataclasses import dataclass
import signal


_cancel_event: ContextVar[Event | None] = ContextVar("read_tool_cancel_event", default=None)
_termination: ContextVar["TerminationState | None"] = ContextVar("cli_termination", default=None)


@dataclass
class TerminationState:
    signum: int | None = None


def termination_requested():
    """停止信号属于整次 CLI 运行；普通 Ctrl+C 仍只取消当前轮次。"""
    state = _termination.get()
    return state is not None and state.signum is not None


@contextmanager
def termination_scope():
    """主线程临时接管 SIGTERM，复用 KeyboardInterrupt 的工具清理和历史回滚。"""
    state = TerminationState()
    token = _termination.set(state)
    previous = None
    installed = current_thread() is main_thread()
    def stop(signum, frame):
        if state.signum is None:
            state.signum = signum
            raise KeyboardInterrupt
    try:
        if installed:
            previous = signal.signal(signal.SIGTERM, stop)
        yield state
    finally:
        if installed and previous is not None:
            signal.signal(signal.SIGTERM, previous)
        _termination.reset(token)


class ToolCancelled(KeyboardInterrupt):
    """向上传播取消，避免被普通工具异常处理转成错误结果。"""


@contextmanager
def cancellation_scope(event: Event):
    """临时绑定本线程的共享事件；退出时恢复先前绑定。"""
    token = _cancel_event.set(event)
    try:
        yield
    finally:
        _cancel_event.reset(token)


def check_cancelled() -> None:
    """检查当前任务是否被取消；无取消上下文时不做任何事。"""
    event = _cancel_event.get()
    if event is not None and event.is_set():
        raise ToolCancelled("只读工具任务已取消。")
