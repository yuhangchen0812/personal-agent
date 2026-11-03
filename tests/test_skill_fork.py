"""手动 fork 两条主链路：隔离/权限/恢复，以及 Plan、预算和中断。"""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from personal_agent.main import run_repl
from personal_agent.session import Session
from personal_agent.skills import discover_skills, prepare_skill_call
from personal_agent.tool_registry import TOOL_REGISTRY
from personal_agent.tools import read_file, write_file
from personal_agent.usage import TokenPrices


def call(call_id, name, arguments):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


class ForkSkillSmokeTests(unittest.TestCase):
    def test_named_and_explicit_fork_isolate_methods_and_preserve_parent_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            root = base / "project"
            project = root / ".personal/skills/project review/SKILL.md"
            user_root = base / "home/.personal/skills"
            user = user_root / "review/SKILL.md"
            project.parent.mkdir(parents=True)
            user.parent.mkdir(parents=True)
            project.write_text("---\nname: review\ncontext: fork\nallowed-tools: []\n---\nPRIVATE_PROJECT_METHOD", encoding="utf-8")
            user.write_text("---\nname: review\ndescription: 检查代码\ncontext: fork\nallowed-tools: read_file, write_file, grep_search\n---\nPRIVATE_USER_METHOD $ARGUMENTS\n目录：${CLAUDE_SKILL_DIR}", encoding="utf-8")
            private = root / ".personal/skills/private/SKILL.md"
            private.parent.mkdir()
            private.write_text("---\nuser-invocable: false\ncontext: fork\n---\n内部方法", encoding="utf-8")
            (root / "source.txt").write_text("CURRENT_PROJECT_EVIDENCE", encoding="utf-8")
            arguments = r"source.txt C:\new\1.txt $ARGUMENTS"
            captures = []
            counts = {"child": 0, "parent": 0}

            def model(messages, definitions, *, on_usage):
                child = "skill-fork" in messages[0]["content"]
                role = "child" if child else "parent"
                counts[role] += 1
                captures.append((role, copy.deepcopy(messages)))
                on_usage({"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3})
                if not child:
                    self.assertNotIn("PRIVATE_USER_METHOD", str(messages))
                    self.assertNotIn("PRIVATE_PROJECT_METHOD", str(messages))
                    self.assertNotIn("read_evidence", str(messages))
                    self.assertIn("PARENT_MEMORY", str(messages))
                    self.assertIn("用户技能结论", str(messages))
                    self.assertIn("项目技能结论", str(messages))
                    return {"role": "assistant", "content": "基于两次子任务结论回答"}
                self.assertNotIn("PRIVATE_PARENT", str(messages))
                self.assertNotIn("PARENT_MEMORY", str(messages))
                if counts[role] == 1:
                    self.assertEqual([item["role"] for item in messages], ["system", "user"])
                    self.assertEqual(messages[1]["content"], arguments)
                    self.assertIn(f"PRIVATE_USER_METHOD {arguments}", messages[0]["content"])
                    self.assertIn(f"目录：{user.parent}", messages[0]["content"])
                    self.assertIn("技能作用域：user", messages[0]["content"])
                    self.assertEqual({item["name"] for item in definitions}, {"read_file", "write_file", "grep_search"})
                    # 展开后文件修改不能影响本轮能力与方法快照。
                    user.write_text("---\nname: review\ncontext: fork\nallowed-tools: []\n---\nCHANGED_METHOD", encoding="utf-8")
                    return {"role": "assistant", "tool_calls": [
                        call("read_evidence", "read_file", {"file_path": "source.txt"}),
                        call("write_result", "write_file", {"file_path": "playground/result.txt", "content": "获批结果"}),
                        call("search", "grep_search", {"pattern": "EVIDENCE"}),
                        call("recursive", "agent", {"prompt": "禁止递归"}),
                        call("control", "exit_plan_mode", {}),
                    ]}
                if counts[role] == 2:
                    results = {item["tool_call_id"]: item["content"] for item in messages if item["role"] == "tool"}
                    self.assertIn("CURRENT_PROJECT_EVIDENCE", results["read_evidence"])
                    self.assertIn("已创建", results["write_result"])
                    self.assertIn("显式 deny", results["search"])
                    self.assertIn("skill-fork:review 仅允许", results["recursive"])
                    self.assertIn("skill-fork:review 仅允许", results["control"])
                    return {"role": "assistant", "content": "用户技能结论"}
                self.assertEqual(definitions, [])
                self.assertIn("PRIVATE_PROJECT_METHOD", messages[0]["content"])
                if counts[role] == 3:
                    self.assertEqual([item["role"] for item in messages], ["system", "user"])
                    self.assertEqual(messages[1]["content"], "执行技能方法中描述的任务。")
                    return {"role": "assistant", "tool_calls": [call("blocked_read", "read_file", {"file_path": "source.txt"})]}
                self.assertIn("无（未启用工具）", messages[-1]["content"])
                return {"role": "assistant", "content": "项目技能结论"}

            output, errors = io.StringIO(), io.StringIO()
            reader, writer, blocked = Mock(wraps=read_file), Mock(wraps=write_file), Mock()
            with (
                patch("personal_agent.skills.PROJECT_ROOT", root),
                patch("personal_agent.skills.USER_SKILLS_ROOT", user_root),
                patch("personal_agent.agent_config.PROJECT_ROOT", root),
                patch("personal_agent.agent_config.USER_AGENTS_ROOT", base / "user-agents"),
                patch("personal_agent.tools.PROJECT_ROOT", root),
                patch("personal_agent.memory.PROJECT_ROOT", root),
                patch("personal_agent.session.PROJECT_ROOT", root),
                patch.dict(TOOL_REGISTRY["read_file"], {"handler": reader}),
                patch.dict(TOOL_REGISTRY["write_file"], {"handler": writer}),
                patch.dict(TOOL_REGISTRY["grep_search"], {"handler": blocked}),
                patch("personal_agent.main.get_model_message", side_effect=model) as request,
                patch("personal_agent.main.confirm_in_terminal", return_value=True) as confirm,
                redirect_stdout(output), redirect_stderr(errors),
            ):
                skills, _ = discover_skills()
                with self.assertRaisesRegex(ValueError, "user-invocable"):
                    prepare_skill_call("/private", skills)
                session = Session("fork_skill", with_tools=True)
                session.create()
                original = [{"role": "user", "content": "PRIVATE_PARENT"}, {"role": "assistant", "content": "已有回答"}]
                session.save(original)
                with patch("builtins.input", side_effect=[
                    "/memory save profile PARENT_MEMORY", "/memory use profile", "/skills", "/skills search 检查代码",
                    f"/review {arguments}", f'/skill "{project}"', "根据两次结论回答", "/usage", "exit",
                ]):
                    history = run_repl(session_name="fork_skill", resume=True, deny=["grep_search"])
                self.assertEqual(session.load(), history)
                with patch("builtins.input", side_effect=["exit"]):
                    self.assertEqual(run_repl(session_name="fork_skill", resume=True), history)
                self.assertEqual(request.call_count, 5)
                confirm.assert_called_once()
            self.assertEqual(counts, {"child": 4, "parent": 1})
            self.assertEqual(len(history), 8)
            self.assertEqual(history[:2], original)
            self.assertNotIn("PRIVATE_USER_METHOD", str(history))
            self.assertNotIn("PRIVATE_PROJECT_METHOD", str(history))
            self.assertIn("技能执行方式：fork", history[2]["content"])
            self.assertEqual((root / "playground/result.txt").read_text(encoding="utf-8"), "获批结果")
            reader.assert_called_once()
            writer.assert_called_once()
            blocked.assert_not_called()
            self.assertIn("可手动 fork（需工具聊天）", output.getvalue())
            self.assertIn("逻辑调用 5 次，完整用量 5 次", output.getvalue())
            self.assertEqual(errors.getvalue(), "")

    def test_default_tools_still_obey_plan_budget_cancellation_and_text_mode(self):
        for scenario in ("plan", "budget", "cancel", "text"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                path = root / ".personal/skills/forker/SKILL.md"
                path.parent.mkdir(parents=True)
                path.write_text("---\ncontext: fork\n---\nDEFAULT_FORK_METHOD", encoding="utf-8")
                calls = []

                def model(messages, definitions, *, on_usage):
                    calls.append(copy.deepcopy(messages))
                    on_usage({"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1})
                    if scenario == "cancel":
                        raise KeyboardInterrupt()
                    self.assertEqual({item["name"] for item in definitions}, {"read_file", "list_files", "grep_search", "write_file", "edit_file", "run_shell", "skill"})
                    if len(calls) == 1:
                        return {"role": "assistant", "tool_calls": [call("write", "write_file", {"file_path": "playground/result.txt", "content": "未获批"})]}
                    self.assertIn("权限拒绝", messages[-1]["content"])
                    return {"role": "assistant", "content": "Plan 禁止任务写入"}

                output, errors = io.StringIO(), io.StringIO()
                blocked = Mock()
                with (
                    patch("personal_agent.skills.PROJECT_ROOT", root),
                    patch("personal_agent.skills.USER_SKILLS_ROOT", root / "no-user-skills"),
                    patch("personal_agent.session.PROJECT_ROOT", root),
                    patch("personal_agent.main.get_model_message", side_effect=model),
                    patch.dict(TOOL_REGISTRY["write_file"], {"handler": blocked}),
                    patch("personal_agent.main.confirm_in_terminal") as confirm,
                    patch("builtins.input", side_effect=["/forker", "/usage", "exit"]),
                    redirect_stdout(output), redirect_stderr(errors),
                ):
                    history = run_repl(
                        session_name="fork_errors", with_tools=scenario != "text",
                        permission_mode="plan" if scenario == "plan" else "default",
                        allow=["write_file"] if scenario == "plan" else None,
                        prices=TokenPrices(Decimal(1_000_000), Decimal(1_000_000)),
                        max_cost=Decimal(1 if scenario == "budget" else 10),
                        stream=scenario == "plan",
                    )
                    self.assertEqual(Session("fork_errors").load(), history)
                if scenario == "plan":
                    self.assertEqual(len(calls), 2)
                    self.assertEqual(len(history), 2)
                    self.assertIn("Personal Agent > Plan 禁止任务写入", output.getvalue())
                else:
                    self.assertEqual(history, [])
                    self.assertEqual(len(calls), 0 if scenario == "text" else 1)
                if scenario == "budget":
                    self.assertIn("已达到本次运行估算费用预算", errors.getvalue())
                    self.assertIn("逻辑调用 1 次，完整用量 1 次", output.getvalue())
                elif scenario == "cancel":
                    self.assertIn("已中断本轮任务", output.getvalue())
                elif scenario == "text":
                    self.assertIn("fork 需要工具聊天", errors.getvalue())
                blocked.assert_not_called()
                confirm.assert_not_called()
                self.assertFalse((root / "playground/result.txt").exists())
