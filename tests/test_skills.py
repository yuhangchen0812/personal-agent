"""一个单文件解析流程检查演示、默认值、BOM/换行和关键格式错误。"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from personal_agent.skills import load_skill, main


class SkillParsingSmokeTest(unittest.TestCase):
    def test_single_file_metadata_body_and_explicit_format_errors(self):
        output = io.StringIO()
        with redirect_stdout(output):
            main(["examples/skills/code-review/SKILL.md"])
        definition = json.loads(output.getvalue())
        self.assertEqual(definition["name"], "code-review")
        self.assertEqual(definition["context"], "inline")
        self.assertEqual(definition["allowed_tools"], ["read_file", "grep_search", "list_files"])
        self.assertIn("$ARGUMENTS", definition["prompt_template"])  # 本步不替换模板。
        self.assertIn("${CLAUDE_SKILL_DIR}", definition["prompt_template"])
        self.assertIn("用户要求", definition["when_to_use"])

        with tempfile.TemporaryDirectory() as temporary, patch("personal_agent.skills.PROJECT_ROOT", Path(temporary).resolve()):
            path = Path(temporary).resolve() / ".personal/skills/default-skill/SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_bytes("\ufeff# 方法\r\n    保留正文缩进。\r\n".encode("utf-8"))
            skill = load_skill(".personal/skills/default-skill/SKILL.md")
            self.assertEqual(skill.name, "default-skill")
            self.assertEqual((skill.description, skill.allowed_tools, skill.user_invocable, skill.context), ("", None, True, "inline"))
            self.assertEqual(skill.skill_dir, path.parent)
            self.assertEqual(skill.prompt_template, "# 方法\n    保留正文缩进。")
            path.write_text("---\nname: default-skill\n正文", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "未闭合"):
                load_skill(path)
            path.write_text('---\nallowed-tools: ["read_file", 7]\n---\n正文', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "allowed-tools"):
                load_skill(path)
