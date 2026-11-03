"""list_files 的三个核心行为测试。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from personal_agent.tools import LIST_ERROR_PREFIX, list_files


class ListFilesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        root_patch = patch("personal_agent.tools.PROJECT_ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        for name in ("main.py", "中文 目录/helper.py", "notes.md", ".venv/hidden.py", ".env"):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("demo", encoding="utf-8")

    def test_glob_and_recursive_listing(self):
        self.assertEqual(list_files("*.py"), "main.py")
        self.assertEqual(list_files("**/*.py"), "main.py\n中文 目录/helper.py")
        self.assertEqual(list_files("*.py", path="中文 目录"), "helper.py")

    def test_excluded_results_are_filtered(self):
        self.assertEqual(list_files("**/*"), "main.py\nnotes.md\n中文 目录/helper.py")

    def test_no_match_and_invalid_directory(self):
        self.assertIn("未找到匹配", list_files("*.js"))
        self.assertTrue(list_files("*", path="missing").startswith(LIST_ERROR_PREFIX))
        self.assertTrue(list_files("*", path="..").startswith(LIST_ERROR_PREFIX))


if __name__ == "__main__":
    unittest.main()
