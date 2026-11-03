"""general 的两条主链路：逐操作审批，以及继承父级策略。"""

import io
import json
import shutil
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from personal_agent.main import run_repl
from personal_agent.plan import PlanSession
from personal_agent.session import Session
from personal_agent.subagent import GENERAL_SYSTEM_PROMPT, GENERAL_TOOLS
from personal_agent.tool_registry import TOOL_REGISTRY
from personal_agent.tools import edit_file, run_shell, write_file
from personal_agent.usage import TokenPrices


def call(call_id, name, arguments):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


class GeneralAgentSmokeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell"), "实际 Shell 演示需要 PowerShell")
    def test_general_uses_terminal_approvals_before_real_write_edit_and_shell(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target = root / "playground/result.txt"
            skill = root / ".personal/skills/method/SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("---\nname: method\n---\n按参数核对结果：$ARGUMENTS", encoding="utf-8")
            counts = {"parent": 0, "general": 0}
            main_thread = threading.get_ident()

            def model(messages, definitions, *, on_usage):
                self.assertEqual(threading.get_ident(), main_thread)
                child = any(item.get("content") == GENERAL_SYSTEM_PROMPT for item in messages)
                role = "general" if child else "parent"
                counts[role] += 1
                on_usage({"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12})
                if not child:
                    if counts[role] == 1:
                        return {"role": "assistant", "tool_calls": [call("general_task", "agent", {"prompt": "完成练习写入、编辑与验证", "type": "general"})]}
                    self.assertEqual(messages[-1], {"role": "tool", "tool_call_id": "general_task", "content": "general 已完成获批操作，拒绝操作未执行。"})
                    self.assertNotIn("general_read", str(messages))
                    return {"role": "assistant", "content": "已取得子任务报告。"}
                self.assertEqual({item["name"] for item in definitions}, GENERAL_TOOLS)
                if counts[role] == 1:
                    self.assertEqual([item["role"] for item in messages], ["system", "user"])
                    return {"role": "assistant", "tool_calls": [
                        call("write_no", "write_file", {"file_path": "playground/result.txt", "content": "不能写"}),
                        call("write_yes", "write_file", {"file_path": "playground/result.txt", "content": "old"}),
                        call("edit_yes", "edit_file", {"file_path": "playground/result.txt", "old_string": "old", "new_string": "new"}),
                        call("shell_no", "run_shell", {"command": "Write-Output 'must-not-run'"}),
                        call("shell_yes", "run_shell", {"command": "Write-Output 'general-shell-ok'"}),
                        call("general_read", "read_file", {"file_path": "playground/result.txt"}),
                        call("load", "skill", {"skill_name": "method", "args": "result.txt"}),
                        call("recursive", "agent", {"prompt": "不能递归"}),
                        call("enter", "enter_plan_mode", {}), call("exit", "exit_plan_mode", {}),
                        call("compact", "compact_context", {}),
                    ]}
                results = {item["tool_call_id"]: item["content"] for item in messages if item["role"] == "tool"}
                self.assertIn("未获批准", results["write_no"])
                self.assertIn("未获批准", results["shell_no"])
                self.assertIn("已创建", results["write_yes"])
                self.assertIn("已编辑", results["edit_yes"])
                self.assertIn("general-shell-ok", results["shell_yes"])
                self.assertIn("退出码：0", results["shell_yes"])
                self.assertIn("new", results["general_read"])
                self.assertIn("按参数核对结果：result.txt", results["load"])
                for key in ("recursive", "enter", "exit", "compact"):
                    self.assertIn("general 仅允许", results[key])
                return {"role": "assistant", "content": "general 已完成获批操作，拒绝操作未执行。"}

            def inputs():
                yield "委派 general 完成局部练习任务"
                yield "n"
                self.assertFalse(target.exists())
                yield "y"
                self.assertEqual(target.read_text(encoding="utf-8"), "old")
                yield "y"
                self.assertEqual(target.read_text(encoding="utf-8"), "new")
                yield "n"
                shell.assert_not_called()
                yield "y"
                yield "/usage"
                yield "exit"

            output, errors = io.StringIO(), io.StringIO()
            with (
                patch("personal_agent.tools.PROJECT_ROOT", root),
                patch("personal_agent.skills.PROJECT_ROOT", root),
                patch("personal_agent.agent_config.PROJECT_ROOT", root),
                patch("personal_agent.agent_config.USER_AGENTS_ROOT", root / "user-agents"),
                patch("personal_agent.skills.USER_SKILLS_ROOT", root / "user-skills"),
                patch("personal_agent.session.PROJECT_ROOT", root),
                patch.dict(TOOL_REGISTRY["run_shell"], {"handler": Mock(wraps=run_shell)}) as registry,
                patch("personal_agent.main.get_model_message", side_effect=model),
                patch("builtins.input", side_effect=inputs()),
                redirect_stdout(output), redirect_stderr(errors),
            ):
                shell = registry["handler"]
                history = run_repl(session_name="general", with_tools=True, prices=TokenPrices(Decimal(2), Decimal(8)), max_cost=Decimal("0.01"))
                self.assertEqual(Session("general").load(), history)
                shell.assert_called_once_with(command="Write-Output 'general-shell-ok'", cwd="playground", timeout=30)
            self.assertEqual(counts, {"parent": 2, "general": 2})
            self.assertEqual(len(history), 4)
            self.assertEqual(errors.getvalue(), "")
            self.assertEqual(output.getvalue().count("等待确认："), 5)
            self.assertIn("逻辑调用 4 次，完整用量 4 次，缺失 0 次", output.getvalue())

    def test_general_inherits_plan_dontask_explicit_deny_and_accept_edits(self):
        for mode in ("plan", "dontAsk", "bypassPermissions", "acceptEdits"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                target = root / "playground/keep.txt"
                target.parent.mkdir()
                target.write_bytes(b"old")
                counts, states = {"parent": 0, "general": 0}, []

                def make_plan(mode):
                    state = PlanSession(mode)
                    states.append((state, state.snapshot()))
                    return state

                def model(messages, definitions, *, on_usage):
                    child = any(item.get("content") == GENERAL_SYSTEM_PROMPT for item in messages)
                    role = "general" if child else "parent"
                    counts[role] += 1
                    on_usage({"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12})
                    if not child:
                        if counts[role] == 1:
                            return {"role": "assistant", "tool_calls": [call("task", "agent", {"prompt": "验证父级限制", "type": "general"})]}
                        return {"role": "assistant", "content": "收到受控执行报告"}
                    if counts[role] == 1:
                        return {"role": "assistant", "tool_calls": [
                            call("write", "write_file", {"file_path": "playground/keep.txt", "content": "draft"}),
                            call("edit", "edit_file", {"file_path": "playground/keep.txt", "old_string": "draft", "new_string": "new"}),
                            call("shell", "run_shell", {"command": "Write-Output 'must-not-run'"}),
                            call("outside", "write_file", {"file_path": "personal_agent/outside.txt", "content": "不能越界"}),
                        ]}
                    results = {item["tool_call_id"]: item["content"] for item in messages if item["role"] == "tool"}
                    if mode == "acceptEdits":
                        self.assertIn("已覆盖", results["write"])
                        self.assertIn("已编辑", results["edit"])
                        self.assertIn("未获批准", results["shell"])
                        self.assertIn("只允许写入 playground", results["outside"])
                    else:
                        self.assertTrue(all("权限拒绝" in value for value in results.values()))
                    return {"role": "assistant", "content": "已遵守父级权限"}

                writer, editor, shell = Mock(wraps=write_file), Mock(wraps=edit_file), Mock()
                output, errors = io.StringIO(), io.StringIO()
                with (
                    patch("personal_agent.tools.PROJECT_ROOT", root),
                    patch("personal_agent.skills.PROJECT_ROOT", root),
                    patch("personal_agent.agent_config.PROJECT_ROOT", root),
                    patch("personal_agent.agent_config.USER_AGENTS_ROOT", root / "user-agents"),
                    patch("personal_agent.skills.USER_SKILLS_ROOT", root / "user-skills"),
                    patch("personal_agent.main.PlanSession", side_effect=make_plan),
                    patch.dict(TOOL_REGISTRY["write_file"], {"handler": writer}),
                    patch.dict(TOOL_REGISTRY["edit_file"], {"handler": editor}),
                    patch.dict(TOOL_REGISTRY["run_shell"], {"handler": shell}),
                    patch("personal_agent.main.get_model_message", side_effect=model),
                    patch("personal_agent.main.confirm_in_terminal", return_value=False) as confirm,
                    patch("builtins.input", side_effect=["委派 general", "exit"]),
                    redirect_stdout(output), redirect_stderr(errors),
                ):
                    run_repl(
                        permission_mode=mode,
                        allow=["write_file", "run_shell"] if mode == "plan" else None,
                        deny=["write_file", "edit_file", "run_shell"] if mode == "bypassPermissions" else None,
                    )
                    self.assertEqual(states[0][0].snapshot(), states[0][1])
                self.assertEqual(counts, {"parent": 2, "general": 2})
                self.assertEqual(errors.getvalue(), "")
                shell.assert_not_called()
                self.assertFalse((root / "personal_agent/outside.txt").exists())
                if mode == "acceptEdits":
                    self.assertEqual(target.read_bytes(), b"new")
                    self.assertEqual((writer.call_count, editor.call_count, confirm.call_count), (2, 1, 1))
                    self.assertEqual(confirm.call_args.args[0], "run_shell")
                else:
                    self.assertEqual(target.read_bytes(), b"old")
                    writer.assert_not_called()
                    editor.assert_not_called()
                    confirm.assert_not_called()
