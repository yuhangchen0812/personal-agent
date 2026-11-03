"""一个主链路场景检查清空存档、保留 Plan 与预算、恢复后重新聊天。"""

import copy
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from personal_agent.main import run_repl
from personal_agent.session import Session
from personal_agent.usage import TokenPrices


class ClearSmokeTest(unittest.TestCase):
    def test_clear_persists_without_resetting_budget_or_plan(self):
        requests = []

        def model(messages, *_, on_usage=None, **__):
            requests.append(copy.deepcopy(messages))
            on_usage({"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500})
            return {"role": "assistant", "content": "完整回答。"}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            output, errors = io.StringIO(), io.StringIO()

            def inputs():
                yield "第一问"
                yield "/clear"
                # 在退出时的再次保存之前，确认命令本身已经更新磁盘。
                saved = Session("clear_demo")
                self.assertEqual(saved.load(), [])
                self.assertEqual(saved.plan_snapshot["state"], "planning")
                yield "/cost"
                yield "预算用完仍想继续"
                yield "exit"

            with (
                patch("personal_agent.tools.PROJECT_ROOT", root),
                patch("personal_agent.session.PROJECT_ROOT", root),
                patch("personal_agent.main.get_model_message", side_effect=model) as request,
                redirect_stdout(output), redirect_stderr(errors),
            ):
                with patch("builtins.input", side_effect=inputs()):
                    history = run_repl(session_name="clear_demo", with_tools=True, permission_mode="plan",
                                       prices=TokenPrices(Decimal(2), Decimal(8)), max_cost=Decimal("0.006"))
                request.assert_called_once()
                self.assertEqual(history, [])
                self.assertIn("0.006000 元", output.getvalue())
                self.assertIn("逻辑调用 1 次", output.getvalue())
                self.assertIn("已达到本次运行估算费用预算", errors.getvalue())

                # 新一轮程序运行恢复空历史；统计按原约定从这次运行重新累计。
                with patch("builtins.input", side_effect=["全新问题", "exit"]):
                    resumed = run_repl(session_name="clear_demo", resume=True)
                self.assertEqual([m["content"] for m in requests[-1] if m["role"] == "user"], ["全新问题"])
                self.assertEqual(len(resumed), 2)
                self.assertEqual(Session("clear_demo").load(), resumed)
