"""定向验证协作取消、真实 Shell 清理和中断后的完整历史。"""

import copy
import ctypes
import io
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from contextlib import redirect_stderr, redirect_stdout
from ctypes import wintypes
from pathlib import Path
from unittest.mock import MagicMock, patch

from personal_agent.cancellation import ToolCancelled, cancellation_scope, check_cancelled
from personal_agent.main import run_repl
from personal_agent.session import Session
from personal_agent.tools import _read_text, grep_search, list_files, read_file, run_shell
from test_agent import tool_call


class CancellationTests(unittest.TestCase):
    def test_cancel_active_and_queued_reads_before_later_write_and_resume_chat(self):
        gate = threading.Barrier(4, timeout=5)
        started, stopped, requests = [], [], []
        lock = threading.Lock()
        pause = threading.Event()
        original_result = Future.result
        interrupted = False

        def execute(name, arguments, **_):
            with lock:
                started.append(arguments["file_path"])
            try:
                gate.wait()
                while not pause.wait(0.01):
                    check_cancelled()
            finally:
                with lock:
                    stopped.append(arguments["file_path"])

        def result(future, timeout=None):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                self.assertEqual(timeout, 0.1)
                gate.wait()  # 三个工作线程正在运行，其余任务仍排队。
                raise KeyboardInterrupt
            return original_result(future, timeout=timeout)

        calls = [tool_call(str(index), "read_file", {"file_path": str(index)}) for index in range(9)]
        calls.append(tool_call("later_write", "write_file", {"file_path": "playground/later.txt", "content": "不应执行"}))
        replies = iter([
            {"role": "assistant", "content": "此前完整回答。"},
            {"role": "assistant", "tool_calls": calls},
            {"role": "assistant", "content": "取消后继续回答。"},
        ])

        def model(messages, *_, **__):
            requests.append(copy.deepcopy(messages))
            return next(replies)

        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with (
                patch("personal_agent.tools.PROJECT_ROOT", root),
                patch("personal_agent.session.PROJECT_ROOT", root),
                patch("personal_agent.main.get_model_message", side_effect=model),
                patch("personal_agent.main.execute_tool", side_effect=execute),
                patch.object(Future, "result", new=result),
                patch("builtins.input", side_effect=["之前的问题", "取消读取", "下一问", "exit"]),
                redirect_stdout(output),
            ):
                history = run_repl(session_name="cancel_reads", with_tools=True)
                self.assertEqual(Session("cancel_reads").load(), history)
            self.assertFalse((root / "playground/later.txt").exists())
        self.assertEqual(sorted(started), ["0", "1", "2"])
        self.assertEqual(sorted(stopped), sorted(started))
        self.assertEqual([message["content"] for message in history if message["role"] == "user"], ["之前的问题", "下一问"])
        self.assertFalse(any(message.get("tool_calls") or message["role"] == "tool" for message in requests[-1]))
        self.assertIn("已中断本轮任务", output.getvalue())

    def test_actual_file_tools_stop_at_checkpoints_and_release_files(self):
        event = threading.Event()
        with tempfile.TemporaryDirectory() as temporary, patch("personal_agent.tools.PROJECT_ROOT", Path(temporary).resolve()):
            root = Path(temporary).resolve()
            large = root / "large.txt"
            large.write_text("中文内容\n" * 40000, encoding="utf-8")
            (root / "second.txt").write_text("第二个文件", encoding="utf-8")
            with large.open("r", encoding="utf-8-sig") as source:
                wrapped = MagicMock(wraps=source)
                wrapped.__enter__.return_value = wrapped
                wrapped.__exit__.side_effect = lambda *_: source.close()

                def read(size):
                    chunk = source.read(size)
                    event.set()
                    return chunk

                wrapped.read.side_effect = read
                with cancellation_scope(event), patch("personal_agent.tools.Path.open", return_value=wrapped):
                    with self.assertRaises(ToolCancelled):
                        read_file("large.txt")
                self.assertEqual(wrapped.read.call_count, 1)
                self.assertTrue(source.closed)
            check_cancelled()  # 退出作用域后不污染其他调用。

            event.clear()
            original_glob = Path.glob

            def candidates(path, pattern):
                for index, candidate in enumerate(original_glob(path, pattern)):
                    if index == 1:
                        event.set()
                    yield candidate

            with cancellation_scope(event), patch.object(Path, "glob", new=candidates):
                with self.assertRaises(ToolCancelled):
                    list_files("*.txt")

            event.clear()

            def read_then_cancel(path):
                text = _read_text(path)
                event.set()
                return text

            with cancellation_scope(event), patch("personal_agent.tools._read_text", side_effect=read_then_cancel):
                with self.assertRaises(ToolCancelled):
                    grep_search("中文", path="large.txt")
            self.assertIn("large.txt", list_files("*.txt"))
            self.assertIn("第二个文件", read_file("second.txt"))

    def test_cleanup_failure_is_reported_and_interrupt_still_propagates(self):
        process = MagicMock()
        process.__enter__.return_value = process
        process.communicate.side_effect = KeyboardInterrupt
        errors = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "playground").mkdir()
            with (
                patch("personal_agent.tools.PROJECT_ROOT", root),
                patch("personal_agent.tools.shutil.which", return_value="offline-pwsh"),
                patch("personal_agent.tools.subprocess.Popen", return_value=process),
                patch("personal_agent.tools._stop_shell_process", side_effect=subprocess.TimeoutExpired("cleanup", 5)),
                redirect_stderr(errors),
                self.assertRaises(KeyboardInterrupt),
            ):
                run_shell("不会实际执行")
        self.assertIn("进程清理失败", errors.getvalue())

    @unittest.skipUnless(sys.platform == "win32" and (shutil.which("pwsh") or shutil.which("powershell")), "需要 Windows PowerShell")
    def test_interrupt_kills_real_shell_and_child_then_continues_saved_chat(self):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        original_popen = subprocess.Popen
        original_communicate = original_popen.communicate
        shells = []
        child_handle = None
        interrupted = False

        def spawn(args, **options):
            process = original_popen(args, **options)
            if "-EncodedCommand" in args:
                shells.append(process)
            return process

        def cleanup():
            for process in shells:
                if process.poll() is None:
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
                    process.wait(timeout=5)
        self.addCleanup(cleanup)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            playground = root / "playground"
            playground.mkdir()
            marker = playground / "child.pid"

            def communicate(process, *args, **kwargs):
                nonlocal child_handle, interrupted
                if process in shells and not interrupted:
                    interrupted = True
                    deadline = time.monotonic() + 8
                    pid = None
                    while time.monotonic() < deadline:
                        try:
                            pid = int(marker.read_text(encoding="utf-8"))
                            break
                        except (FileNotFoundError, ValueError):
                            time.sleep(0.02)
                    if pid is None:
                        cleanup()
                        self.fail("外部 Python 子进程未启动")
                    child_handle = kernel.OpenProcess(0x00100001, False, pid)
                    if not child_handle:
                        cleanup()
                        self.fail("无法取得本次子进程句柄")
                    self.addCleanup(kernel.CloseHandle, child_handle)
                    self.addCleanup(kernel.TerminateProcess, child_handle, 1)
                    raise KeyboardInterrupt  # 注入中断；Shell 和 Python 子进程都是真实进程。
                return original_communicate(process, *args, **kwargs)

            python = sys.executable.replace("'", "''")
            code = 'import os,pathlib,time; pathlib.Path("child.pid").write_text(str(os.getpid()),encoding="utf-8"); time.sleep(60)'
            command = f"& '{python}' -X utf8 -c '{code}'"
            replies = iter([
                {"role": "assistant", "content": "已有回答。"},
                {"role": "assistant", "tool_calls": [
                    tool_call("shell", "run_shell", {"command": command}),
                    tool_call("later", "write_file", {"file_path": "playground/later.txt", "content": "不能执行"}),
                ]},
                {"role": "assistant", "content": "中断后继续。"},
            ])
            with (
                patch("personal_agent.tools.PROJECT_ROOT", root),
                patch("personal_agent.session.PROJECT_ROOT", root),
                patch("personal_agent.main.get_model_message", side_effect=lambda *_, **__: next(replies)),
                patch("personal_agent.tools.subprocess.Popen", side_effect=spawn),
                patch.object(original_popen, "communicate", new=communicate),
                patch("builtins.input", side_effect=["第一问", "运行长命令", "y", "继续", "exit"]),
                redirect_stdout(io.StringIO()),
            ):
                history = run_repl(session_name="cancel_shell", with_tools=True)
                self.assertEqual(Session("cancel_shell").load(), history)
            self.assertEqual(len(history), 4)
            self.assertEqual(history[-1]["content"], "中断后继续。")
            self.assertFalse((playground / "later.txt").exists())
            self.assertTrue(child_handle)
            self.assertEqual(kernel.WaitForSingleObject(child_handle, 5000), 0)
            self.assertIsNotNone(shells[0].poll())
            self.assertTrue(shells[0].stdout.closed)
            self.assertTrue(shells[0].stderr.closed)


if __name__ == "__main__":
    unittest.main()
