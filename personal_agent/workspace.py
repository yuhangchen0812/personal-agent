"""文件工具的调用级工程范围；普通聊天继续使用各模块原 PROJECT_ROOT。"""

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path


_workspace = ContextVar("file_tool_workspace", default=None)


def project_root(default: Path) -> Path:
    return _workspace.get() or default.resolve()


@contextmanager
def tool_workspace(root: Path):
    root = Path(root).absolute()
    if root.resolve() != root or not root.is_dir():
        raise ValueError("工具工程须为已创建的原始绝对目录，不能通过链接重定向。")
    token = _workspace.set(root)
    try:
        yield
    finally:
        _workspace.reset(token)
