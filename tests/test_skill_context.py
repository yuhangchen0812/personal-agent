"""一条候选上下文链路：同轮快照、工具后请求、历史隔离与失败继续。"""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from personal_agent.main import run_repl
from personal_agent.memory import save_memory
from personal_agent.session import Session
from personal_agent.skill_search import retrieve_relevant_skills


class SkillContextSmokeTest(unittest.TestCase):
    def test_candidate_metadata_is_request_only_and_stable_within_task(self):
        requests = []
        replies = iter([
            {"role": "assistant", "tool_calls": [{"id": "read", "type": "function", "function": {
                "name": "read_file", "arguments": '{"file_path":"source.txt"}',
            }}]},
            {"role": "assistant", "content": "已读取并检查。"},
            {"role": "assistant", "content": "已列出测试用例。"},
            {"role": "assistant", "content": "无相关候选。"},
            {"role": "assistant", "content": "手动调用完成。"},
            {"role": "assistant", "content": "候选失败后继续回答。"},
            {"role": "assistant", "content": "无效摘要，用于确认摘要请求未被注入候选。"},
        ])
        question = "检查代码异常处理并读取 source.txt"
        failure_question = "候选检索失败时继续回答"

        def rank(query, skills):
            if query == failure_question:
                raise OSError("模拟检索失败")
            return retrieve_relevant_skills(query, skills)

        def last_user(messages):
            return next(message for message in reversed(messages) if message["role"] == "user")

        def candidates(messages):
            content = last_user(messages)["content"]
            return json.loads(content.split("<retrieved_skills>\n", 1)[1].split("\n</retrieved_skills>", 1)[0])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            user_root = root / "user-home/.personal/skills"
            review = user_root / "review/SKILL.md"
            review.parent.mkdir(parents=True)
            review.write_text("---\nname: code-review\ndescription: 检查代码异常处理\nwhen-to-use: 检查代码时\n---\n正文独有标记：此处是完整方法。", encoding="utf-8")
            tests = root / ".personal/skills/write-tests/SKILL.md"
            tests.parent.mkdir(parents=True)
            tests.write_text("---\ndescription: 设计测试用例\n---\n测试正文独有标记。", encoding="utf-8")
            (root / "source.txt").write_text("实际源代码", encoding="utf-8")

            def model(messages, *_, **__):
                requests.append(copy.deepcopy(messages))
                if len(requests) == 1:
                    review.write_text("---\nname: code-review\ndescription: 已修改的说明\n---\n正文独有标记：新版本。", encoding="utf-8")
                return next(replies)

            def inputs():
                yield "/memory use 编码"
                yield question
                self.assertEqual(Session("skill_context").load()[0]["content"], question)
                yield "设计测试用例"
                yield "volcanicunicorn"
                yield "/code-review 手动检查"
                yield failure_question
                yield "/compact"
                yield "exit"

            output, errors = io.StringIO(), io.StringIO()
            with (
                patch("personal_agent.skills.PROJECT_ROOT", root),
                patch("personal_agent.skills.USER_SKILLS_ROOT", user_root),
                patch("personal_agent.memory.PROJECT_ROOT", root),
                patch("personal_agent.session.PROJECT_ROOT", root),
                patch("personal_agent.tools.PROJECT_ROOT", root),
                patch("personal_agent.main.retrieve_relevant_skills", side_effect=rank) as retrieve,
                patch("personal_agent.main.get_model_message", side_effect=model),
                patch("builtins.input", side_effect=inputs()),
                redirect_stdout(output), redirect_stderr(errors),
            ):
                save_memory("编码", "项目统一使用 UTF-8。")
                history = run_repl(session_name="skill_context", with_tools=True, permission_mode="plan")
                self.assertEqual(Session("skill_context").load(), history)
                self.assertEqual(retrieve.call_count, 4)  # 每个普通任务一次，手动技能和摘要跳过。

            self.assertEqual(len(requests), 7)
            self.assertEqual(candidates(requests[0]), candidates(requests[1]))
            self.assertEqual(candidates(requests[0])[0]["name"], "code-review")
            self.assertEqual(candidates(requests[0])[0]["source"], "user")
            self.assertEqual(candidates(requests[0])[0]["description"], "检查代码异常处理")
            self.assertEqual(candidates(requests[2])[0]["name"], "write-tests")
            for request in requests[:3]:
                current = last_user(request)["content"]
                self.assertEqual(current.count("<retrieved_skills>"), 1)
                self.assertEqual(current.count("<project_memory>"), 1)
                self.assertNotIn("正文独有标记", current)
                self.assertTrue(any(m["role"] == "system" and "planning" in m["content"] for m in request))
            self.assertIn("实际源代码", requests[1][-1]["content"])
            self.assertEqual([m for m in requests[2] if m["role"] == "user"][0]["content"], question)
            for request in requests[3:]:
                self.assertNotIn("<retrieved_skills>", str(request))
            self.assertIn("正文独有标记", last_user(requests[4])["content"])
            self.assertNotIn("<project_memory>", str(requests[6]))  # 摘要只看到存储历史。
            self.assertIn("模拟检索失败", errors.getvalue())
            self.assertEqual(sum(m["role"] == "user" for m in history), 5)
            self.assertNotIn("<retrieved_skills>", str(history))
            self.assertNotIn("<project_memory>", str(history))
