"""write_file 的三个核心行为测试，仅操作临时项目。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from personal_agent.tools import WRITE_ERROR_PREFIX, read_file, write_file


class WriteFileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        root_patch = patch("personal_agent.tools.PROJECT_ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)

    def test_create_nested_utf8_file_and_preview(self):
        relative = "playground/中文 目录/demo.txt"
        result = write_file(relative, "你好\n第二行\n")
        self.assertIn("已创建", result)
        self.assertIn("2 行", result)
        self.assertIn("   1 | 你好", result)
        self.assertEqual((self.root / relative).read_bytes(), "你好\n第二行\n".encode("utf-8"))
        self.assertIn("   2 | 第二行", read_file(relative))

    def test_overwrite_and_empty_content(self):
        relative = "playground/demo.txt"
        write_file(relative, "旧内容\n")
        result = write_file(relative, "更新\n")
        self.assertIn("已覆盖", result)
        self.assertEqual((self.root / relative).read_text(encoding="utf-8"), "更新\n")
        self.assertIn("0 行", write_file(relative, ""))
        self.assertEqual((self.root / relative).read_bytes(), b"")

    def test_blocked_path_and_invalid_content_do_not_write(self):
        self.assertIn("只允许写入 playground", write_file("personal_agent/blocked.txt", "demo"))
        self.assertFalse((self.root / "personal_agent").exists())
        result = write_file("playground/bad.txt", ["not a string"])
        self.assertTrue(result.startswith(WRITE_ERROR_PREFIX))
        self.assertFalse((self.root / "playground/bad.txt").exists())
