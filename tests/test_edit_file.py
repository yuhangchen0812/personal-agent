"""edit_file 的三个核心行为测试，仅操作临时项目。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from personal_agent.tools import EDIT_ERROR_PREFIX, edit_file


class EditFileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        root_patch = patch("personal_agent.tools.PROJECT_ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.relative = "playground/中文 目录/demo.txt"
        self.file = self.root / self.relative
        self.file.parent.mkdir(parents=True)
        self.file.write_bytes("第一行\n状态：待修改\n最后一行\n".encode("utf-8"))

    def test_unique_replacement_and_diff(self):
        result = edit_file(self.relative, "状态：待修改", "状态：已修改")
        self.assertEqual(self.file.read_bytes(), "第一行\n状态：已修改\n最后一行\n".encode("utf-8"))
        self.assertIn(f"--- a/{self.relative}", result)
        self.assertIn(f"+++ b/{self.relative}", result)
        self.assertIn("-状态：待修改", result)
        self.assertIn("+状态：已修改", result)

    def test_missing_and_multiple_matches_leave_bytes_unchanged(self):
        self.file.write_text("重复 重复\n", encoding="utf-8")
        before = self.file.read_bytes()
        self.assertIn("未找到", edit_file(self.relative, "不存在", "新内容"))
        self.assertEqual(self.file.read_bytes(), before)
        self.assertIn("多处匹配", edit_file(self.relative, "重复", "新内容"))
        self.assertEqual(self.file.read_bytes(), before)

    def test_invalid_request_and_outside_path_do_not_modify(self):
        before = self.file.read_bytes()
        self.assertTrue(edit_file(self.relative, "", "新内容").startswith(EDIT_ERROR_PREFIX))
        self.assertTrue(edit_file(self.relative, "状态", None).startswith(EDIT_ERROR_PREFIX))
        self.assertEqual(self.file.read_bytes(), before)
        outside = self.root / "outside.txt"
        outside.write_text("保持原样", encoding="utf-8")
        self.assertIn("只允许写入 playground", edit_file("outside.txt", "保持", "更改"))
        self.assertEqual(outside.read_text(encoding="utf-8"), "保持原样")
        self.assertIn("文件不存在", edit_file("playground/missing.txt", "旧", "新"))
        self.assertFalse((self.root / "playground/missing.txt").exists())
