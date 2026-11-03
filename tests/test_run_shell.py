"""run_shell 的三个核心测试，仅执行固定命令。"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from personal_agent.tools import SHELL_ERROR_PREFIX, run_shell


@unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell"), "需要 PowerShell")
class RunShellTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "playground").mkdir()
        root_patch = patch("personal_agent.tools.PROJECT_ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)

    def test_success_reports_chinese_output_and_working_directory(self):
        result = run_shell("(Get-Location).Path; Write-Output '你好，Shell！'")
        self.assertIn("退出码：0", result)
        self.assertIn("你好，Shell！", result)
        self.assertIn(str(self.root / "playground"), result)

    def test_nonzero_exit_and_invalid_directory(self):
        result = run_shell("Write-Output 'normal'; [Console]::Error.WriteLine('demo error'); exit 7")
        self.assertIn("退出码：7", result)
        self.assertIn("normal", result)
        self.assertIn("demo error", result)
        with patch("personal_agent.tools.subprocess.Popen") as spawn:
            self.assertTrue(run_shell("Write-Output 'unused'", cwd=".").startswith(SHELL_ERROR_PREFIX))
        spawn.assert_not_called()

    def test_external_child_command_times_out(self):
        python_path = sys.executable.replace("'", "''")
        command = f"& '{python_path}' -X utf8 -c 'import time; print(\"waiting\", flush=True); time.sleep(5)'"
        result = run_shell(command, timeout=1)
        self.assertIn("命令超时（1 秒）", result)
        self.assertIn("退出码：不可用（超时终止）", result)
