"""一条 CLI 主流程检查自动触发、快照去重、折叠后的失败回滚及预算。"""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from personal_agent.context import SUMMARY_PREFIX, context_chars
from personal_agent.session_memory import generate_memory
from personal_agent.main import main
from personal_agent.plan import PlanSession
from personal_agent.session import Session


class AutoCompactSmokeTest(unittest.TestCase):
    def test_cli_auto_compaction_deduplicates_and_keeps_committed_history_on_failure(self):
        original = [
            {"role": "user", "content": "目标整理报告，约束只读，标识 A-23。" + "背景材料。" * 500},
            {"role": "assistant", "tool_calls": [{"id": "old_read", "type": "function", "function": {
                "name": "read_file", "arguments": '{"file_path":"source.txt"}',
            }}]},
            {"role": "tool", "tool_call_id": "old_read", "content": "   1 | 样例。"},
            {"role": "assistant", "content": "已完成读取。"},
            {"role": "user", "content": "保留最近一轮。"},
            {"role": "assistant", "content": "仍在等待批准。"},
        ]
        second_question = "第二问仍只读。" + "最近任务详情。" * 200
        from codex.verify_session_memory import memory
        summary = memory()
        summary["episode_memory"]["task_description"] = "整理报告 A-23。"
        requests = []

        def model(messages, *definitions, on_usage=None, **options):
            requests.append(copy.deepcopy(messages))
            on_usage({"prompt_tokens": 900, "completion_tokens": 100, "total_tokens": 1000})
            number = len(requests)
            if number in (1, 4):
                self.assertEqual(definitions, ())
                self.assertEqual(options["max_tokens"], 1024)
                return {"role": "assistant", "content": "坏摘要" if number == 1 else json.dumps(summary, ensure_ascii=False)}
            self.assertTrue(any(m["role"] == "system" and "planning" in m["content"] for m in messages))
            if number in (2, 5):
                raise RuntimeError("模拟普通请求失败")
            return {"role": "assistant", "content": "完整回答，继续保持只读。"}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            output, errors = io.StringIO(), io.StringIO()
            with (
                patch("personal_agent.tool_output.PROJECT_ROOT", root),
                patch("personal_agent.tools.PROJECT_ROOT", root),
                patch("personal_agent.session.PROJECT_ROOT", root),
                patch("personal_agent.main.get_model_message", side_effect=model) as request,
                patch("personal_agent.main.generate_memory", wraps=generate_memory) as compact,
                redirect_stdout(output), redirect_stderr(errors),
            ):
                session = Session("auto_demo", with_tools=True)
                session.create()
                session.save(original, plan=PlanSession("plan").snapshot())

                def inputs():
                    yield "第一问"  # 摘要失败，普通请求也失败，原历史保留。
                    self.assertEqual(session.load(), original)
                    yield second_question  # 相同历史不重复摘要，普通请求成功。
                    self.assertEqual(compact.call_count, 1)
                    self.assertEqual(request.call_count, 3)
                    yield "第三问"  # 历史变化后折叠成功，随后普通请求失败。
                    saved = Session("auto_demo")
                    committed = saved.load()
                    self.assertTrue(committed[0]["content"].startswith(SUMMARY_PREFIX))
                    self.assertEqual(committed[-2:], [
                        {"role": "user", "content": second_question},
                        {"role": "assistant", "content": "完整回答，继续保持只读。"},
                    ])
                    self.assertGreater(context_chars(committed), 1000)
                    self.assertEqual(saved.plan_snapshot["state"], "planning")
                    yield "第四问"  # 候选仍超阈值但快照未变，不再次折叠。
                    self.assertEqual(compact.call_count, 2)
                    self.assertEqual(request.call_count, 6)
                    before_block = session.load()
                    yield "第五问"  # 折叠与普通请求都被预算拦截。
                    self.assertEqual(session.load(), before_block)
                    self.assertEqual(request.call_count, 6)
                    yield "/usage"
                    yield "exit"

                with patch("builtins.input", side_effect=inputs()):
                    main(["--resume", "auto_demo", "--auto-compact-chars", "1000",
                          "--input-price", "1", "--output-price", "1", "--max-cost", "0.006"])
        self.assertEqual(len(requests), 6)
        self.assertIn("自动折叠已启用", output.getvalue())
        self.assertIn("逻辑调用 6 次", output.getvalue())
        self.assertIn("自动折叠失败", errors.getvalue())
        self.assertIn("已达到本次运行估算费用预算", errors.getvalue())
