"""一个词法检索流程：中文命中、同名合并、无命中及本地 CLI。"""

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from personal_agent.main import run_repl
from personal_agent.session import Session
from personal_agent.skill_search import retrieve_relevant_skills, tokenize_skill_text
from personal_agent.skills import discover_skills


class SkillSearchSmokeTest(unittest.TestCase):
    def test_chinese_ranking_and_search_command_do_not_execute_skills(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            root = base / "project"
            user_root = base / "home/.personal/skills"

            def write_skill(directory, folder, content):
                path = directory / folder / "SKILL.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")

            write_skill(root / ".personal/skills", "old-review", "---\nname: code-review\ndescription: obsoletelegacyword\n---\n旧的项目方法")
            write_skill(user_root, "review", "---\nname: code-review\ndescription: 检查代码与异常处理\nwhen-to-use: 检查代码错误并说明异常\n---\n先读取 Python 代码，再报告异常处理依据。")
            write_skill(root / ".personal/skills", "write-tests", "---\ndescription: 设计测试用例\nuser-invocable: false\n---\n覆盖测试输入与预期结果。")
            write_skill(root / ".personal/skills", "file-reader", "---\ndescription: Read files and explain file content\n---\n读取文件内容。")
            output = io.StringIO()
            with (
                patch("personal_agent.skills.PROJECT_ROOT", root),
                patch("personal_agent.skills.USER_SKILLS_ROOT", user_root),
                patch("personal_agent.session.PROJECT_ROOT", root),
                patch("personal_agent.main.get_model_message", side_effect=AssertionError("检索不应请求模型")) as model,
                redirect_stdout(output), redirect_stderr(io.StringIO()),
            ):
                skills, warnings = discover_skills()
                self.assertFalse(warnings)
                query = "帮我检查代码里的异常处理"
                tokens = tokenize_skill_text(query)
                self.assertIn("检查", tokens)
                self.assertIn("异常", tokens)
                self.assertNotIn("帮我", tokens)
                self.assertEqual(tokenize_skill_text("FILES"), ["files", "file"])
                hits = retrieve_relevant_skills(query, skills, limit=1)
                self.assertEqual(len(hits), 1)
                self.assertEqual((hits[0].skill.name, hits[0].skill.source), ("code-review", "user"))
                self.assertIn("异常", hits[0].matched_tokens)
                self.assertTrue(0 < hits[0].score <= 1)
                self.assertGreater(hits[0].raw_score, 0)
                self.assertEqual(retrieve_relevant_skills("code-review", skills)[0].name_bonus, 0.15)
                self.assertEqual(retrieve_relevant_skills("FILES", skills)[0].skill.name, "file-reader")
                self.assertEqual(retrieve_relevant_skills("obsoletelegacyword", skills), [])
                self.assertEqual(retrieve_relevant_skills("volcanicunicorn", skills), [])
                self.assertEqual(retrieve_relevant_skills("帮我", skills), [])
                with self.assertRaisesRegex(ValueError, "limit"):
                    retrieve_relevant_skills(query, skills, limit=0)

                with patch("builtins.input", side_effect=[
                    f"/skills search {query}", "/skills search FILES",
                    "/skills search volcanicunicorn", "/skills search", "exit",
                ]):
                    history = run_repl(session_name="skill_search", with_tools=True)
                self.assertEqual(history, [])
                self.assertEqual(Session("skill_search").load(), [])
                model.assert_not_called()

            text = output.getvalue()
            self.assertIn("1. code-review 分数=", text)
            self.assertIn("命中词：", text)
            self.assertIn("BM25 合计=", text)
            self.assertIn("来源：用户", text)
            self.assertIn("1. file-reader 分数=", text)
            self.assertIn("未匹配到达到阈值的技能", text)
            self.assertIn("用法：/skills；/skills search 查询", text)
