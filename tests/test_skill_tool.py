"""一条模型 skill 工具链：加载正文、错误配对、原权限和会话恢复。"""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from personal_agent.main import run_repl
from personal_agent.permissions import PERMISSION_MODES, check_permission
from personal_agent.session import Session
from personal_agent.skills import discover_skills, load_inline_skill, prepare_named_skill
from personal_agent.tool_registry import TOOL_ERROR_PREFIX, TOOL_REGISTRY, execute_tool


class SkillToolSmokeTest(unittest.TestCase):
    def test_model_loads_internal_skill_without_granting_task_permissions(self):
        handler = Mock()
        with patch.dict(TOOL_REGISTRY["skill"], {"handler": handler}):
            for arguments, options in [
                ({}, {}),
                ({"skill_name": "code-review", "args": 7}, {}),
                ({"skill_name": "code-review"}, {"deny": ["skill"]}),
            ]:
                self.assertTrue(execute_tool("skill", arguments, **options).startswith(TOOL_ERROR_PREFIX))
        handler.assert_not_called()
        for mode in PERMISSION_MODES:
            self.assertEqual(check_permission("skill", mode)["action"], "allow")

        def call(call_id, name, arguments):
            return {"id": call_id, "type": "function", "function": {
                "name": name, "arguments": json.dumps(arguments, ensure_ascii=False),
            }}

        arguments = r"source.txt C:\new\1.txt"
        replies = iter([
            {"role": "assistant", "tool_calls": [
                call("load", "skill", {"skill_name": "code-review", "args": arguments}),
                call("missing", "skill", {"skill_name": "missing"}),
            ]},
            {"role": "assistant", "tool_calls": [
                call("read", "read_file", {"file_path": "source.txt"}),
                call("write", "write_file", {"file_path": "playground/result.txt", "content": "不应写入"}),
            ]},
            {"role": "assistant", "content": "按方法检查完成；Plan 拒绝任务写入。"},
        ])
        requests, definitions = [], []

        def model(messages, tools, **__):
            requests.append(copy.deepcopy(messages))
            definitions.append(copy.deepcopy(tools))
            return next(replies)

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            root, user_root = base / "project", base / "home/.personal/skills"
            old = root / ".personal/skills/review/SKILL.md"
            old.parent.mkdir(parents=True)
            old.write_text("---\nname: code-review\n---\n旧项目方法不应被加载", encoding="utf-8")
            skill_file = user_root / "review/SKILL.md"
            skill_file.parent.mkdir(parents=True)
            skill_file.write_text(
                '---\nname: code-review\ndescription: 检查代码异常处理\nuser-invocable: false\nallowed-tools: ["write_file"]\n---\n'
                '先读取参数：$ARGUMENTS\n目录：${CLAUDE_SKILL_DIR}\n正文独有标记：给出检查依据。',
                encoding="utf-8",
            )
            fork = root / ".personal/skills/fork-review/SKILL.md"
            fork.parent.mkdir()
            fork.write_text("---\ncontext: fork\n---\nfork 正文不应返回", encoding="utf-8")
            (root / "source.txt").write_text("真实源文件内容", encoding="utf-8")
            question = "检查代码异常处理，按相关技能方法读取 source.txt"
            with (
                patch("personal_agent.skills.PROJECT_ROOT", root),
                patch("personal_agent.skills.USER_SKILLS_ROOT", user_root),
                patch("personal_agent.tools.PROJECT_ROOT", root),
                patch("personal_agent.session.PROJECT_ROOT", root),
                patch("personal_agent.main.get_model_message", side_effect=model) as request,
                patch("personal_agent.main.confirm_in_terminal") as confirm,
                patch("builtins.input", side_effect=[question, "exit"]),
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()),
            ):
                skills, _ = discover_skills()
                with self.assertRaisesRegex(ValueError, "user-invocable"):
                    prepare_named_skill("/code-review 手动调用", skills)
                self.assertIn("仅支持 inline", load_inline_skill("fork-review"))
                self.assertIn("需要父聊天运行时", execute_tool("skill", {"skill_name": "fork-review"}))
                history = run_repl(session_name="skill_tool", with_tools=True, permission_mode="plan")
                self.assertEqual(Session("skill_tool").load(), history)
                confirm.assert_not_called()
                with patch("builtins.input", side_effect=["exit"]):
                    restored = run_repl(session_name="skill_tool", resume=True)
                self.assertEqual(restored, history)
                self.assertEqual(request.call_count, 3)

            self.assertIn("skill", [tool["name"] for tool in definitions[0]])
            current_user = next(m["content"] for m in requests[0] if m["role"] == "user")
            self.assertIn("<retrieved_skills>", current_user)
            self.assertNotIn("正文独有标记", current_user)
            results = [m for m in requests[1] if m["role"] == "tool"]
            self.assertEqual([m["tool_call_id"] for m in results], ["load", "missing"])
            self.assertIn(f"先读取参数：{arguments}", results[0]["content"])
            self.assertIn(f"目录：{skill_file.parent}", results[0]["content"])
            self.assertIn("技能作用域：user", results[0]["content"])
            self.assertIn("正文独有标记", results[0]["content"])
            self.assertNotIn("旧项目方法", results[0]["content"])
            self.assertIn("未知技能", results[1]["content"])
            followup = [m for m in requests[2] if m["role"] == "tool"]
            self.assertIn("真实源文件内容", followup[-2]["content"])
            self.assertIn("权限拒绝", followup[-1]["content"])
            self.assertIn("plan", followup[-1]["content"])
            self.assertFalse((root / "playground/result.txt").exists())
            self.assertEqual(history[0], {"role": "user", "content": question})
            self.assertNotIn("<retrieved_skills>", str(history))
            self.assertIn("正文独有标记", str(history))
