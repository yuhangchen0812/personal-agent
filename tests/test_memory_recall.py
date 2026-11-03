"""一个主流程检查按问题匹配、无命中、手动覆盖以及关闭。"""

import copy
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from personal_agent.main import run_repl
from personal_agent.memory import read_memory, save_memory, select_relevant_memory


class MemoryRecallSmokeTest(unittest.TestCase):
    def test_auto_selection_switches_per_question_and_manual_use_overrides(self):
        requests = []

        def model(messages, *_, **__):
            requests.append(copy.deepcopy(messages))
            return {"role": "assistant", "content": "固定回答。"}

        with tempfile.TemporaryDirectory() as temporary, patch("personal_agent.memory.PROJECT_ROOT", Path(temporary).resolve()):
            save_memory("编码习惯", "正文：统一使用 UTF-8。", description="Python 文件编码约定")
            save_memory("验证方式", "正文：只验证主流程。", description="测试验证范围约定")
            with (
                patch("personal_agent.memory.read_memory", wraps=read_memory) as read,
                patch("personal_agent.main.select_relevant_memory", wraps=select_relevant_memory) as select,
                patch("personal_agent.main.get_model_message", side_effect=model),
                patch("builtins.input", side_effect=[
                    "/memory auto", "Python 文件编码怎么设置？", "验证测试范围怎么定？", "天气如何？",
                    "/memory use 编码习惯", "验证测试范围？", "/memory off", "Python 文件编码？", "exit",
                ]), redirect_stdout(io.StringIO()),
            ):
                history = run_repl()
                self.assertEqual(select.call_count, 3)
                self.assertEqual([call.args[0] for call in read.call_args_list], ["编码习惯", "验证方式"])
        questions = [[m for m in messages if m["role"] == "user"][-1]["content"] for messages in requests]
        self.assertEqual(len(questions), 5)
        self.assertIn("正文：统一使用 UTF-8。", questions[0])
        self.assertIn("正文：只验证主流程。", questions[1])
        self.assertNotIn("<project_memory>", questions[2])
        self.assertIn("正文：统一使用 UTF-8。", questions[3])
        self.assertNotIn("<project_memory>", questions[4])
        self.assertTrue(all("<project_memory>" not in message["content"] for message in history))
