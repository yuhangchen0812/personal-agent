"""一个主流程验证手动记忆跨运行存取、清空历史保留及项目隔离。"""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from personal_agent.main import run_repl
from personal_agent.memory import list_memories, read_memory


class MemorySmokeTest(unittest.TestCase):
    def test_manual_memory_survives_clear_and_restart_in_its_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project_a, project_b = root / "project-a", root / "project-b"
            output = io.StringIO()
            with patch("personal_agent.main.get_model_message") as model, redirect_stdout(output):
                with patch("personal_agent.memory.PROJECT_ROOT", project_a):
                    with patch("builtins.input", side_effect=[
                        "/memory save 编码习惯 Python 使用 UTF-8。", "/clear", "exit",
                    ]):
                        self.assertEqual(run_repl(), [])
                    files = list((project_a / ".personal/memory").glob("memory-*.md"))
                    self.assertEqual(len(files), 1)
                    self.assertIn('name: "编码习惯"', files[0].read_text(encoding="utf-8"))
                    # 新建一次交互运行，仍可从磁盘读取，不依赖上一轮消息。
                    with patch("builtins.input", side_effect=["/memory", "/memory read 编码习惯", "exit"]):
                        self.assertEqual(run_repl(), [])
                    self.assertEqual(read_memory("编码习惯")["content"], "Python 使用 UTF-8。")
                with patch("personal_agent.memory.PROJECT_ROOT", project_b):
                    self.assertEqual(list_memories(), [])
                    with self.assertRaises(ValueError):
                        read_memory("编码习惯")
                model.assert_not_called()
            self.assertIn("编码习惯 [project]", output.getvalue())
            self.assertIn("Python 使用 UTF-8。", output.getvalue())
