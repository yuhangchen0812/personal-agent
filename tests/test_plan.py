"""Plan 的三个核心行为测试；文件实际读写，模型响应固定。"""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from personal_agent.main import run_repl
from personal_agent.plan import PlanSession
from personal_agent.tool_registry import TOOL_REGISTRY, execute_tool


def tool_call(call_id, name, arguments):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


class PlanTests(unittest.TestCase):
    def test_only_exact_plan_file_can_change_before_approval(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch("personal_agent.tools.PROJECT_ROOT", root):
                plan = PlanSession("plan")
                result = execute_tool("write_file", {"file_path": plan.plan_file, "content": "步骤一：创建练习文件。\n"}, plan=plan)
                self.assertIn("已创建", result)
                equivalent = str(root / plan.plan_file)
                result = execute_tool("edit_file", {"file_path": equivalent, "old_string": "步骤一", "new_string": "第一步"}, plan=plan)
                self.assertIn("已编辑", result)
                result = execute_tool("read_file", {"file_path": plan.plan_file}, plan=plan)
                self.assertIn("第一步", result)
                handler = Mock()
                with patch.dict(TOOL_REGISTRY["write_file"], {"handler": handler}):
                    for target in [plan.plan_file + ".other", "playground/task.txt"]:
                        self.assertIn("权限拒绝", execute_tool("write_file", {"file_path": target, "content": "不执行"}, plan=plan, mode="bypassPermissions", allow=["write_file"]))
                    self.assertIn("权限拒绝", execute_tool("write_file", {"file_path": plan.plan_file, "content": "不执行"}, plan=plan, deny=["write_file"]))
                handler.assert_not_called()
                shell = Mock()
                with patch.dict(TOOL_REGISTRY["run_shell"], {"handler": shell}):
                    self.assertIn("权限拒绝", execute_tool("run_shell", {"command": "Write-Output 'blocked'"}, plan=plan, allow=["run_shell"]))
                shell.assert_not_called()

    def test_submission_requires_actual_plan_and_explicit_approval(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch("personal_agent.tools.PROJECT_ROOT", root):
                plan = PlanSession("plan")
                self.assertIn("先写入计划", execute_tool("exit_plan_mode", {}, plan=plan))
                execute_tool("write_file", {"file_path": plan.plan_file, "content": "\n"}, plan=plan)
                self.assertIn("正文为空", execute_tool("exit_plan_mode", {}, plan=plan))
                execute_tool("write_file", {"file_path": plan.plan_file, "content": "创建演示文件。\n"}, plan=plan)
                self.assertIn("未提供回调", execute_tool("exit_plan_mode", {}, plan=plan))
                self.assertEqual((plan.state, plan.mode), ("awaiting_approval", "plan"))
                self.assertIn("权限拒绝", execute_tool("write_file", {"file_path": plan.plan_file, "content": "等待中不能修改"}, plan=plan))
                self.assertIn("未获批准", execute_tool("exit_plan_mode", {}, plan=plan, confirm=lambda *_: False))
                self.assertEqual((plan.state, plan.mode), ("planning", "plan"))

                def approve(name, arguments, reason):
                    self.assertEqual(plan.state, "awaiting_approval")
                    self.assertEqual(name, "exit_plan_mode")
                    self.assertEqual(arguments["plan"], "创建演示文件。\n")
                    self.assertEqual(arguments["execution_mode"], "default")
                    return True

                result = execute_tool("exit_plan_mode", {}, plan=plan, confirm=approve)
                self.assertIn("计划已批准", result)
                self.assertEqual((plan.state, plan.mode), ("executing", "default"))
                self.assertIn("未提供审批回调", execute_tool("write_file", {"file_path": "playground/task.txt", "content": "仍需单次审批"}, plan=plan))
                self.assertFalse((root / "playground/task.txt").exists())
                self.assertIn("不支持的参数", execute_tool("exit_plan_mode", {"approved": True}, plan=plan))
                self.assertIn("状态对象", execute_tool("enter_plan_mode", {}))

    def test_repl_rejects_then_revises_approves_and_executes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch("personal_agent.tools.PROJECT_ROOT", root):
                plan = PlanSession("acceptEdits")
                requests = []

                def responses():
                    yield {"role": "assistant", "tool_calls": [tool_call("enter", "enter_plan_mode", {})]}
                    yield {"role": "assistant", "tool_calls": [
                        tool_call("plan_v1", "write_file", {"file_path": plan.plan_file, "content": "创建任务文件：初稿。\n"}),
                        tool_call("submit_v1", "exit_plan_mode", {}),
                        tool_call("blocked", "write_file", {"file_path": "playground/blocked.txt", "content": "不得写入"}),
                    ]}
                    yield {"role": "assistant", "content": "计划被拒绝，等待修改意见。"}
                    yield {"role": "assistant", "tool_calls": [
                        tool_call("plan_v2", "edit_file", {"file_path": plan.plan_file, "old_string": "初稿", "new_string": "修订后"}),
                        tool_call("submit_v2", "exit_plan_mode", {}),
                        tool_call("execute", "write_file", {"file_path": "playground/result.txt", "content": "修订后执行成功。\n"}),
                    ]}
                    yield {"role": "assistant", "content": "已按批准的计划执行。"}

                scripted = responses()

                def model(messages, definitions, **_):
                    requests.append(copy.deepcopy(messages))
                    self.assertIn("exit_plan_mode", [tool["name"] for tool in definitions])
                    return next(scripted)

                output = io.StringIO()
                with (
                    patch("personal_agent.main.PlanSession", return_value=plan),
                    patch("personal_agent.main.get_model_message", side_effect=model),
                    patch("builtins.input", side_effect=["请先规划再执行", "n", "修改计划后重提", "y", "exit"]),
                    redirect_stdout(output),
                ):
                    history = run_repl(permission_mode="acceptEdits")

                results = {message["tool_call_id"]: message["content"] for message in history if message["role"] == "tool"}
                self.assertIn("未获批准", results["submit_v1"])
                self.assertIn("权限拒绝", results["blocked"])
                self.assertIn("计划已批准", results["submit_v2"])
                self.assertEqual((plan.state, plan.mode), ("executing", "acceptEdits"))
                self.assertFalse((root / "playground/blocked.txt").exists())
                self.assertEqual((root / "playground/result.txt").read_text(encoding="utf-8"), "修订后执行成功。\n")
                self.assertIn("planning", requests[1][0]["content"])
                self.assertIn("executing", requests[-1][0]["content"])
                self.assertIn("修订后", output.getvalue())
                self.assertEqual(len(results), 7)
