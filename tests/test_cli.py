"""命令行启动参数的两个核心测试，不请求模型。"""

import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from personal_agent.main import main


class CliTests(unittest.TestCase):
    def test_default_and_custom_limit_reach_repl(self):
        with patch("personal_agent.main.run_repl") as repl:
            main([])
            repl.assert_called_once_with(max_turns=5, permission_mode="default", allow=None, deny=None, session_name=None, resume=False, with_tools=False, stream=False, prices=None, max_cost=None, auto_compact_chars=None)
            repl.reset_mock()
            main(["--max-turns", "2"])
            repl.assert_called_once_with(max_turns=2, permission_mode="default", allow=None, deny=None, session_name=None, resume=False, with_tools=False, stream=False, prices=None, max_cost=None, auto_compact_chars=None)
            repl.reset_mock()
            main(["--permission-mode", "plan", "--allow-tool", "write_file", "--deny-tool", "run_shell"])
            repl.assert_called_once_with(max_turns=5, permission_mode="plan", allow=["write_file"], deny=["run_shell"], session_name=None, resume=False, with_tools=False, stream=False, prices=None, max_cost=None, auto_compact_chars=None)
            repl.reset_mock()
            main(["--session", "study"])
            repl.assert_called_once_with(max_turns=5, permission_mode="default", allow=None, deny=None, session_name="study", resume=False, with_tools=False, stream=False, prices=None, max_cost=None, auto_compact_chars=None)
            repl.reset_mock()
            main(["--resume", "study"])
            repl.assert_called_once_with(max_turns=5, permission_mode="default", allow=None, deny=None, session_name="study", resume=True, with_tools=False, stream=False, prices=None, max_cost=None, auto_compact_chars=None)
            repl.reset_mock()
            main(["--input-price", "2", "--output-price", "8", "--max-cost", "0.01"])
            self.assertEqual(str(repl.call_args.kwargs["max_cost"]), "0.01")

    def test_invalid_limit_fails_before_start(self):
        errors = io.StringIO()
        with patch("personal_agent.main.run_repl") as repl, redirect_stderr(errors):
            with self.assertRaises(SystemExit) as result:
                main(["--max-turns", "0"])
        self.assertEqual(result.exception.code, 2)
        repl.assert_not_called()
        self.assertIn("--max-turns 必须是正整数", errors.getvalue())
        with patch("personal_agent.main.run_repl") as repl, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                main(["--session", "one", "--resume", "two"])
        self.assertEqual(result.exception.code, 2)
        repl.assert_not_called()
