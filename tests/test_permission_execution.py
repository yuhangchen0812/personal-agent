"""执行侧权限与审批的三个核心测试，不请求模型。"""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from personal_agent.agent import run_agent_loop
from personal_agent.permissions import confirm_in_terminal
from personal_agent.tool_registry import TOOL_ERROR_PREFIX, TOOL_REGISTRY, execute_tool


class PermissionExecutionTests(unittest.TestCase):
    def test_denied_and_unapproved_calls_do_not_execute(self):
        arguments = {"file_path": "playground/demo.txt", "content": "demo"}
        handler = Mock()
        with patch.dict(TOOL_REGISTRY["write_file"], {"handler": handler}):
            self.assertIn("未提供审批回调", execute_tool("write_file", arguments))
            self.assertIn("未获批准", execute_tool("write_file", arguments, confirm=lambda *_: False))
            with patch("builtins.input", side_effect=EOFError), redirect_stdout(io.StringIO()):
                self.assertIn("未获批准", execute_tool("write_file", arguments, confirm=confirm_in_terminal))
            self.assertIn("权限拒绝", execute_tool("write_file", arguments, mode="bypassPermissions", deny=["write_file"]))
        handler.assert_not_called()
        approval = Mock(return_value=True)
        shell = Mock()
        with patch.dict(TOOL_REGISTRY["run_shell"], {"handler": shell}):
            self.assertIn("plan", execute_tool("run_shell", {"command": "Write-Output 'demo'"}, mode="plan", allow=["run_shell"], confirm=approval))
        approval.assert_not_called()
        shell.assert_not_called()

    def test_approved_calls_execute_and_keep_path_checks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch("personal_agent.tools.PROJECT_ROOT", root):
                approval = Mock(return_value=True)
                result = execute_tool("write_file", {"file_path": "playground/demo.txt", "content": "旧内容\n"}, confirm=approval)
                self.assertIn("已创建", result)
                approval.assert_called_once()
                result = execute_tool("edit_file", {"file_path": "playground/demo.txt", "old_string": "旧内容", "new_string": "新内容"}, mode="acceptEdits")
                self.assertIn("已编辑", result)
                self.assertEqual((root / "playground/demo.txt").read_text(encoding="utf-8"), "新内容\n")
                result = execute_tool("write_file", {"file_path": "personal_agent/blocked.txt", "content": "demo"}, mode="bypassPermissions")
                self.assertIn("只允许写入 playground", result)
                self.assertFalse((root / "personal_agent").exists())
        shell = Mock(return_value="shell-result")
        with patch.dict(TOOL_REGISTRY["run_shell"], {"handler": shell}):
            self.assertEqual(execute_tool("run_shell", {"command": "Write-Output 'demo'"}, confirm=lambda *_: True), "shell-result")
        shell.assert_called_once_with(command="Write-Output 'demo'", cwd="playground", timeout=30)

    def test_agent_pairs_denied_result_before_continuing(self):
        requests = []
        responses = iter([
            {"role": "assistant", "tool_calls": [{"id": "denied_1", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"file_path": "playground/demo.txt", "content": "demo"})}}]},
            {"role": "assistant", "content": "该操作已被权限规则拒绝。"},
        ])
        def model(messages, definitions):
            requests.append(copy.deepcopy(messages))
            return next(responses)
        def executor(name, arguments):
            return execute_tool(name, arguments, mode="plan", allow=["write_file"])
        handler = Mock()
        messages = [{"role": "user", "content": "写文件"}]
        with patch.dict(TOOL_REGISTRY["write_file"], {"handler": handler}):
            reply = run_agent_loop(messages, model, tool_executor=executor)
        handler.assert_not_called()
        self.assertEqual(requests[1][-1]["tool_call_id"], "denied_1")
        self.assertTrue(requests[1][-1]["content"].startswith(TOOL_ERROR_PREFIX))
        self.assertIn("plan", requests[1][-1]["content"])
        self.assertEqual(reply, "该操作已被权限规则拒绝。")
