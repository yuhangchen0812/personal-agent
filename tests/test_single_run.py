"""单次任务的两个主要场景；固定模型，实际文件和启动脚本。"""

from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx2
from openai import AuthenticationError

from codex.verify_single_run import invoke, tool, verify_offline
from personal_agent.main import main
from personal_agent.session import Session


class SingleRunTests(unittest.TestCase):
    def test_cli_outputs_permissions_resume_and_launchers(self):
        self.assertEqual(verify_offline()["status"], "passed")

    def test_failure_cancellation_budget_save_and_argument_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with (
                patch("personal_agent.main.get_model_settings", return_value={"protocol": "openai", "model": "fixed-model", "base_url": "https://example.invalid"}),
                patch("personal_agent.tools.PROJECT_ROOT", root), patch("personal_agent.session.PROJECT_ROOT", root),
                patch("personal_agent.main.discover_skills", return_value=([], [])),
                patch("personal_agent.main.discover_agents", return_value=([], [])),
                patch("builtins.input", side_effect=AssertionError("must not prompt")),
            ):
                with patch("personal_agent.main.get_model_message") as model:
                    code, output, _ = invoke(["--prompt", "任务", "--output-format", "json", "--input-price", "1", "--output-price", "1", "--max-cost", "0"])
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(output)["usage"]["calls"], 0)
                model.assert_not_called()
                error = AuthenticationError("private-key-body", response=httpx2.Response(401, request=httpx2.Request("POST", "https://example.invalid")), body={"secret": "private-key-body"})
                with patch("personal_agent.main.get_model_message", side_effect=error):
                    code, output, errors = invoke(["--prompt", "任务", "--output-format", "json"])
                self.assertEqual(code, 1)
                self.assertNotIn("private-key-body", output + errors)
                self.assertIsNone(json.loads(output)["usage"]["total_tokens"])

                calls = []
                def cancel(messages, definitions=None, *, on_usage=None, **_):
                    calls.append(messages)
                    if len(calls) == 2:
                        raise KeyboardInterrupt()
                    on_usage({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                    return {"role": "assistant", "tool_calls": [tool("written", "write_file", {"file_path": "playground/written.txt", "content": "已发生副作用"})]}
                with patch("personal_agent.main.get_model_message", side_effect=cancel):
                    code, output, _ = invoke(["--prompt", "写入任务", "--output-format", "json", "--permission-mode", "acceptEdits", "--session", "cancelled", "--with-tools"])
                result = json.loads(output)
                self.assertEqual(code, 130)
                self.assertEqual(result["status"], "cancelled")
                self.assertEqual(result["tool_trace"][0]["outcome"], "returned")
                self.assertEqual(result["usage"]["missing_calls"], 1)
                self.assertEqual(len(calls), 2)
                self.assertEqual(Session("cancelled").load(), [])
                self.assertEqual((root / "playground/written.txt").read_text(encoding="utf-8"), "已发生副作用")

                def text(messages, definitions=None, *, on_usage=None, **_):
                    on_usage({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                    return {"role": "assistant", "content": "文本结果"}
                with patch("personal_agent.main.get_model_message", side_effect=text), patch("personal_agent.main.Session.save", side_effect=OSError("save unavailable")):
                    code, output, _ = invoke(["--prompt", "纯文本", "--output-format", "json", "--session", "save-fail"])
                self.assertEqual(code, 1)
                result = json.loads(output)
                self.assertEqual(result["answer"], "文本结果")
                self.assertEqual(result["error"]["phase"], "session_save")
                self.assertFalse(result["session"]["saved"])
                with patch("sys.stdin", io.StringIO('多行\n"输入"')), patch("personal_agent.main.get_model_message", side_effect=text):
                    code, output, _ = invoke(["--prompt-file", "-", "--output-format", "json"])
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(output)["answer"], "文本结果")
            with patch("personal_agent.main.run_once") as runner, redirect_stderr(io.StringIO()):
                for arguments in (["--output-format", "json"], ["--prompt", " "], ["--prompt-file", str(root / "missing")]):
                    with self.assertRaises(SystemExit) as status:
                        main(arguments)
                    self.assertEqual(status.exception.code, 2)
                runner.assert_not_called()
