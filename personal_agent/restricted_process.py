"""Windows Job 内运行固定测试命令：进程/内存、墙钟和日志上限；不是安全沙箱。"""

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import threading
import time


MAX_PROCESSES = 8
JOB_MEMORY_BYTES = 512 * 1024 * 1024


def execution_limits(timeout, max_output):
    return {"backend": "windows_job", "max_processes": MAX_PROCESSES, "job_memory_bytes": JOB_MEMORY_BYTES,
        "timeout_seconds": timeout, "max_output_bytes": max_output, "inherited_environment": "essential Windows variables only",
        "filesystem_network_sandbox": False}


class _BasicLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong), ("flags", wintypes.DWORD),
        ("min_working", ctypes.c_size_t), ("max_working", ctypes.c_size_t), ("processes", wintypes.DWORD),
        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", _BasicLimits), ("io", _IoCounters), ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]


class WindowsJob:
    def __init__(self):
        if os.name != "nt":
            raise ValueError("当前配置测试执行器仅支持 Windows Job，不回退到无约束执行。")
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, result, args in (
            ("CreateJobObjectW", wintypes.HANDLE, [wintypes.LPVOID, wintypes.LPCWSTR]),
            ("SetInformationJobObject", wintypes.BOOL, [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]),
            ("AssignProcessToJobObject", wintypes.BOOL, [wintypes.HANDLE, wintypes.HANDLE]),
            ("OpenProcess", wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]),
            ("CloseHandle", wintypes.BOOL, [wintypes.HANDLE])):
            function = getattr(self.api, name)
            function.restype, function.argtypes = result, args
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.basic.flags = 0x2000 | 0x0008 | 0x0200  # KILL_ON_JOB_CLOSE / ACTIVE_PROCESS / JOB_MEMORY
        limits.basic.processes = MAX_PROCESSES
        limits.job_memory = JOB_MEMORY_BYTES
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def attach(self, pid):
        handle = self.api.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA / TERMINATE
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self.api.AssignProcessToJobObject(self.handle, handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.api.CloseHandle(handle)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def run_limited(command, *, cwd, environment, runtime, timeout, max_output, xml_path):
    """子进程先等 stdin 门闩；Job 绑定完成才允许运行测试，失败不降级。"""
    runtime = Path(runtime)
    runtime.mkdir(parents=True, exist_ok=False)
    (runtime / "tmp").mkdir()
    (runtime / "home").mkdir()
    log = runtime / "pytest.log"
    process, reader, job = None, None, WindowsJob()
    exceeded = threading.Event()
    output = bytearray()
    read_errors = []
    started = time.monotonic()
    status, interrupted = "completed", None
    try:
        process = subprocess.Popen(command, cwd=cwd, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        job.attach(process.pid)
        def drain():
            try:
                while chunk := process.stdout.read1(4096):
                    available = max_output - len(output)
                    output.extend(chunk[:max(0, available)])
                    if len(chunk) > available:
                        exceeded.set()
            except (OSError, ValueError) as error:
                read_errors.append(type(error).__name__)
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        process.stdin.write(b"1")
        process.stdin.close()
        while process.poll() is None:
            if exceeded.is_set():
                status = "output_limit"
                break
            if Path(xml_path).exists() and Path(xml_path).stat().st_size > 1024 * 1024:
                status = "xml_limit"
                break
            if time.monotonic() - started >= timeout:
                status = "timeout"
                break
            time.sleep(0.03)
    except KeyboardInterrupt as error:
        status, interrupted = "cancelled", error
    finally:
        # 正常退出也关闭 Job，回收测试遗留的后代；不依赖父 PID 仍然存在。
        job.close()
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            if reader is not None:
                reader.join(timeout=5)
                if reader.is_alive():
                    raise RuntimeError("测试输出管道未能回收。")
            process.stdout.close()
        log.write_bytes(output)
    if interrupted is not None:
        raise interrupted
    if read_errors:
        raise OSError("测试日志读取失败。")
    if exceeded.is_set():
        status = "output_limit"
    if Path(xml_path).exists() and Path(xml_path).stat().st_size > 1024 * 1024:
        status = "xml_limit"
    return {"status": status, "command": command, "directory": str(cwd), "returncode": process.returncode,
        "log": str(log), "xml": str(xml_path), "elapsed_seconds": round(time.monotonic() - started, 3),
        "limits": execution_limits(timeout, max_output)}
