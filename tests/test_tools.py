"""在临时项目中验证只读文件工具，不读取真实密钥，也不调用模型。"""

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from personal_agent.tools import ERROR_PREFIX, read_file
from personal_agent.workspace import tool_workspace


class ReadFileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        root_patch = patch("personal_agent.tools.PROJECT_ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.sample = self.root / "中文 目录" / "学习 示例.txt"
        self.sample.parent.mkdir()
        self.sample.write_text("第一行\n第二行\n\n第四行\n第五行\n", encoding="utf-8")
        self.relative = "中文 目录/学习 示例.txt"

    def test_chinese_spaces_and_read_does_not_change_bytes(self):
        before = hashlib.sha256(self.sample.read_bytes()).hexdigest()
        self.assertEqual(
            read_file(self.relative),
            "   1 | 第一行\n   2 | 第二行\n   3 | \n   4 | 第四行\n   5 | 第五行",
        )
        self.assertEqual(hashlib.sha256(self.sample.read_bytes()).hexdigest(), before)

    def test_middle_page_has_original_line_numbers_and_continuation(self):
        self.assertEqual(
            read_file(self.relative, offset=2, limit=2),
            "   2 | 第二行\n   3 | \n\n[显示第 2-3 行，共 5 行。 继续读取：offset=4, limit=2。]",
        )

    def test_last_page_and_oversized_limit(self):
        result = read_file(self.relative, offset=4, limit=100)
        self.assertEqual(result, "   4 | 第四行\n   5 | 第五行\n\n[显示第 4-5 行，共 5 行。]")
        self.assertNotIn("继续读取", result)

    def test_default_returns_at_most_200_lines(self):
        self.sample.write_text("\n".join(str(i) for i in range(1, 202)), encoding="utf-8")
        result = read_file(self.relative)
        self.assertIn(" 200 | 200\n", result)
        self.assertNotIn(" 201 |", result)
        self.assertIn("offset=201, limit=200", result)

    def test_empty_file(self):
        self.sample.write_bytes(b"")
        self.assertEqual(read_file(self.relative), "(空文件)")

    def test_offset_after_eof(self):
        result = read_file(self.relative, offset=6)
        self.assertTrue(result.startswith(ERROR_PREFIX))
        self.assertIn("共 5 行", result)

    def test_utf8_bom_and_newlines(self):
        for content in ("甲\n乙\n", "甲\r\n乙\r\n", "\ufeff甲\n乙"):
            with self.subTest(content=repr(content)):
                self.sample.write_bytes(content.encode("utf-8"))
                self.assertEqual(read_file(self.relative), "   1 | 甲\n   2 | 乙")

    def test_missing_file_and_directory(self):
        self.assertIn("文件不存在", read_file("missing.txt"))
        self.assertIn("路径指向目录", read_file("中文 目录"))

    def test_invalid_file_path(self):
        for file_path in ("", "  ", None, 42, True, Path("sample.txt"), "bad\x00.txt"):
            with self.subTest(file_path=repr(file_path)):
                self.assertTrue(read_file(file_path).startswith(ERROR_PREFIX))

    def test_invalid_offsets_and_limits(self):
        for name in ("offset", "limit"):
            for value in (0, -1, True, False, 1.5, "1", None):
                with self.subTest(name=name, value=value):
                    result = read_file(self.relative, **{name: value})
                    self.assertTrue(result.startswith(ERROR_PREFIX))
                    self.assertIn(name, result)

    def test_absolute_path_and_dot_segments_inside_project(self):
        expected = read_file(self.relative)
        self.assertEqual(read_file(str(self.sample)), expected)
        self.assertEqual(read_file("中文 目录/../中文 目录/学习 示例.txt"), expected)

    def test_outside_project_is_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            other = Path(outside) / "outside.txt"
            other.write_text("outside-test-content", encoding="utf-8")
            for file_path in (str(other), "../outside.txt"):
                with self.subTest(file_path=file_path):
                    result = read_file(file_path)
                    self.assertIn("只允许读取", result)
                    self.assertNotIn("outside-test-content", result)

    def test_excluded_paths_and_env_files(self):
        for relative in (
            "Reference/secret.txt", "REFERENCE/secret2.txt",
            ".venv/secret.txt", ".git/secret.txt", ".personal/secret.txt",
            "nested/__pycache__/secret.txt", ".env", ".env.local",
            "nested/.ENV.PRODUCTION", "nested/.env/settings.txt",
        ):
            with self.subTest(relative=relative):
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("dummy-private-content", encoding="utf-8")
                result = read_file(relative)
                self.assertTrue(result.startswith(ERROR_PREFIX))
                self.assertNotIn("dummy-private-content", result)

    def test_env_example_is_allowed(self):
        (self.root / ".env.example").write_text("EXAMPLE_KEY=", encoding="utf-8")
        self.assertEqual(read_file(".env.example"), "   1 | EXAMPLE_KEY=")

    def test_special_windows_paths_are_rejected(self):
        for relative in (".env::$DATA", "note.txt:extra"):
            with self.subTest(relative=relative):
                self.assertTrue(read_file(relative).startswith(ERROR_PREFIX))

    def test_invalid_utf8_and_null_bytes(self):
        self.sample.write_bytes(b"\xff\xfe\x80")
        self.assertIn("不是有效的 UTF-8", read_file(self.relative))
        self.sample.write_bytes(b"binary\x00content")
        self.assertIn("可能是二进制文件", read_file(self.relative))

    def test_operating_system_errors_are_readable(self):
        for error, expected in (
            (PermissionError("dummy-detail"), "没有权限"),
            (OSError("dummy-detail"), "无法访问文件（OSError）"),
        ):
            with self.subTest(error=type(error).__name__):
                with patch("personal_agent.tools.Path.open", side_effect=error):
                    result = read_file(self.relative)
                self.assertIn(expected, result)
                self.assertNotIn("dummy-detail", result)

    def test_resolved_link_destination_is_checked(self):
        # 只模拟解析后的目标，不把它当作真实 Windows 链接验证。
        # 先固定实际工作区，避免全局 Path.resolve 模拟同时改写工作区根。
        with tool_workspace(self.root):
            with patch("personal_agent.tools.Path.resolve", return_value=self.root.parent / "outside.txt"):
                self.assertIn("只允许读取", read_file("link.txt"))
            with patch("personal_agent.tools.Path.resolve", return_value=self.root / ".env"):
                self.assertIn("不能读取 .env", read_file("link.txt"))


if __name__ == "__main__":
    unittest.main()
