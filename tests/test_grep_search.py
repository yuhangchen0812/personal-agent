"""grep_search 的三个核心行为测试。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from personal_agent.tools import SEARCH_ERROR_PREFIX, grep_search


class GrepSearchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        root_patch = patch("personal_agent.tools.PROJECT_ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        contents = {
            "main.py": "# demo\ndef main():\n    pass\n",
            "中文 目录/helper.py": 'def greet(name):\n    return "你好"\n',
            "说明.txt": "第一行\n你好，Personal Agent\n",
            ".venv/hidden.py": "hidden-marker\n",
            ".env": "hidden-marker\n",
        }
        for name, content in contents.items():
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    def test_regex_and_include(self):
        self.assertEqual(
            grep_search(r"^def\s+\w+", include="*.py"),
            "main.py:2:def main():\n中文 目录/helper.py:1:def greet(name):",
        )
        self.assertEqual(
            grep_search("你好", include="**/*.py"),
            '中文 目录/helper.py:2:    return "你好"',
        )

    def test_single_file_chinese_and_no_match(self):
        self.assertEqual(
            grep_search("你好", path="说明.txt"),
            "说明.txt:2:你好，Personal Agent",
        )
        self.assertEqual(grep_search("not-present", path="说明.txt"), "未找到匹配内容。")

    def test_errors_and_excluded_files(self):
        self.assertIn("正则表达式无效", grep_search("["))
        self.assertTrue(grep_search("demo", path="missing").startswith(SEARCH_ERROR_PREFIX))
        self.assertEqual(grep_search("hidden-marker"), "未找到匹配内容。")


if __name__ == "__main__":
    unittest.main()
