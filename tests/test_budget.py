"""只检查达到预算与缺失用量时的拦截主链路，不请求真实模型。"""

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from unittest.mock import patch

from personal_agent.main import run_repl
from personal_agent.usage import TokenPrices


class BudgetSmokeTest(unittest.TestCase):
    def test_reached_or_unknown_budget_blocks_tools_and_further_requests(self):
        for usage, reason in [
            ({"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500}, "已达到本次运行估算费用预算"),
            (None, "缺少完整用量"),
        ]:
            with self.subTest(reason=reason):
                def model(*_, on_usage=None, **__):
                    on_usage(usage)
                    return {"role": "assistant", "tool_calls": [{
                        "id": "blocked_write", "type": "function",
                        "function": {"name": "write_file", "arguments": '{"file_path":"playground/blocked.txt","content":"不能写"}'},
                    }]}

                output, errors = io.StringIO(), io.StringIO()
                with (
                    patch("personal_agent.main.get_model_message", side_effect=model) as request,
                    patch("personal_agent.main.execute_tool") as execute,
                    patch("builtins.input", side_effect=["执行任务", "继续", "/cost", "exit"]),
                    redirect_stdout(output), redirect_stderr(errors),
                ):
                    history = run_repl(prices=TokenPrices(Decimal(2), Decimal(8)), max_cost=Decimal("0.006"))
                request.assert_called_once()
                execute.assert_not_called()
                self.assertEqual(history, [])
                self.assertIn(reason, errors.getvalue())
                self.assertIn("本次运行估算预算：0.006 元", output.getvalue())
                self.assertIn("逻辑调用 1 次", output.getvalue())
