"""一个固定场景检查 Plan 跨运行恢复、审批前保存与当前权限。"""

import copy
import io
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from personal_agent.main import run_repl
from personal_agent.plan import PlanSession, validate_plan_snapshot
from personal_agent.session import Session
from test_tool_session import call


class PlanSessionPersistenceTests(unittest.TestCase):
    def test_pending_plan_restores_without_old_permissions_or_approval(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            requests = []
            saved_path = None

            def responses():
                nonlocal saved_path
                yield {"role": "assistant", "tool_calls": [call("enter", "enter_plan_mode", {})]}
                saved_path = re.search(r"playground/\.plans/plan-[0-9a-f]{32}\.md", requests[-1][0]["content"]).group()
                yield {"role": "assistant", "tool_calls": [call("plan", "write_file", {"file_path": saved_path, "content": "创建 playground/result.txt，写入固定文本。\n"})]}
                yield {"role": "assistant", "content": "计划已写入，等待批准。"}
                yield {"role": "assistant", "tool_calls": [call("submit", "exit_plan_mode", {})]}
                yield {"role": "assistant", "tool_calls": [
                    call("blocked", "write_file", {"file_path": "playground/blocked.txt", "content": "未批准不能写"}),
                    call("resubmit", "exit_plan_mode", {}),
                    call("write", "write_file", {"file_path": "playground/result.txt", "content": "需要本次确认"}),
                ]}
                yield {"role": "assistant", "content": "计划已批准，写入被拒绝。"}

            replies = responses()
            def model(messages, definitions, **_):
                requests.append(copy.deepcopy(messages))
                return next(replies)

            with (
                patch("personal_agent.session.PROJECT_ROOT", root),
                patch("personal_agent.tools.PROJECT_ROOT", root),
                patch("personal_agent.main.get_model_message", side_effect=model),
                redirect_stdout(io.StringIO()),
            ):
                with patch("builtins.input", side_effect=["先规划", "exit"]):
                    first = run_repl(session_name="plan_demo", with_tools=True, permission_mode="acceptEdits")
                session = Session("plan_demo")
                self.assertEqual(session.load(), first)
                self.assertEqual(session.plan_snapshot["state"], "planning")
                self.assertNotIn("mode", session.plan_snapshot)

                def stop_at_approval(*_):
                    # 模拟在审批界面直接结束进程；此时磁盘必须已是 awaiting_approval。
                    session.load()
                    self.assertEqual(session.plan_snapshot["state"], "awaiting_approval")
                    raise SystemExit("模拟审批处退出")

                with patch("builtins.input", side_effect=["提交计划"]), patch("personal_agent.main.confirm_in_terminal", side_effect=stop_at_approval):
                    with self.assertRaises(SystemExit):
                        run_repl(session_name="plan_demo", resume=True)
                self.assertIn("planning", requests[3][0]["content"])
                (root / saved_path).unlink()  # 计划丢失时从已存正文恢复。
                with patch("builtins.input", side_effect=["继续审批", "y", "n", "exit"]):
                    final = run_repl(session_name="plan_demo", resume=True)
                self.assertIn("awaiting_approval", requests[4][0]["content"])
                self.assertEqual(final[:len(first)], first)
                self.assertEqual((root / saved_path).read_text(encoding="utf-8"), "创建 playground/result.txt，写入固定文本。\n")
                self.assertFalse((root / "playground/blocked.txt").exists())
                self.assertFalse((root / "playground/result.txt").exists())
                self.assertIn("default", final[-3]["content"])  # 原 acceptEdits 没有继承。
                self.assertIn("未获批准", final[-2]["content"])
                session.load()
                self.assertEqual(session.plan_snapshot["state"], "executing")
                with self.assertRaises(ValueError):
                    validate_plan_snapshot({"state": "planning", "plan_file": "playground/other.txt", "content": "不能恢复任意路径"})
                original = root / saved_path
                data = {"state": "planning", "plan_file": saved_path, "content": "与现有正文不同"}
                with self.assertRaisesRegex(ValueError, "不同"):
                    PlanSession().restore(data)
                self.assertEqual(original.read_text(encoding="utf-8"), "创建 playground/result.txt，写入固定文本。\n")
