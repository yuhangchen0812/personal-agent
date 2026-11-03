"""一个固定场景检查 CLI 单价到 /cost 的主链路，不访问模型。"""

import copy
import io
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from unittest.mock import patch

from personal_agent.main import main
from personal_agent.usage import parse_token_price


class CostSmokeTest(unittest.TestCase):
    def test_cli_prices_and_local_cost_command(self):
        requests = []

        def model(messages, *_, on_usage=None, **__):
            requests.append(copy.deepcopy(messages))
            on_usage({"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500})
            return {"role": "assistant", "content": "固定回复。"}

        output = io.StringIO()
        with (
            patch("personal_agent.main.get_model_message", side_effect=model) as request,
            patch("builtins.input", side_effect=["/cost", "你好", "/cost", "exit"]),
            redirect_stdout(output),
        ):
            main(["--input-price", "2", "--output-price", "8"])
        request.assert_called_once()
        self.assertEqual(requests, [[{"role": "user", "content": "你好"}]])
        self.assertIn("暂无完整服务端用量", output.getvalue())
        self.assertIn("0.006000 元", output.getvalue())

        output = io.StringIO()
        with patch("personal_agent.main.get_model_message") as request, patch("builtins.input", side_effect=["/cost", "exit"]), redirect_stdout(output):
            main([])
        request.assert_not_called()
        self.assertIn("估算费用：未知", output.getvalue())
        self.assertEqual(parse_token_price("0"), Decimal(0))
        with self.assertRaises(ValueError):
            parse_token_price("NaN")
