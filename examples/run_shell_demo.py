"""固定命令演示 stdout、stderr、退出码和超时，不请求模型。"""

import sys

from personal_agent.tools import run_shell


def main() -> None:
    print("成功命令与中文输出：")
    print(run_shell("(Get-Location).Path; Write-Output '你好，Shell！'"))
    print("\n错误输出与非零退出码：")
    print(run_shell("Write-Output '普通输出'; [Console]::Error.WriteLine('演示错误'); exit 7"))
    python_path = sys.executable.replace("'", "''")
    command = f"& '{python_path}' -X utf8 -c 'import time; print(\"waiting\", flush=True); time.sleep(5)'"
    print("\n外部子进程超时：")
    print(run_shell(command, timeout=1))


if __name__ == "__main__":
    main()
