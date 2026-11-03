"""一条工具聊天检查请求登记、轮次边界、失败保留与权限和预算。"""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from personal_agent.context import SUMMARY_PREFIX
from personal_agent.main import run_repl
from personal_agent.plan import PlanSession
from personal_agent.session import Session
from personal_agent.tool_registry import TOOL_ERROR_PREFIX, execute_tool
from personal_agent.usage import TokenPrices


class CompactToolSmokeTest(unittest.TestCase):
    def test_tool_queues_once_and_compacts_only_after_completed_task(self):
        callback = Mock(return_value="已登记")
        self.assertTrue(execute_tool("compact_context", {}, mode="plan", deny=["compact_context"],
                                     request_compaction=callback).startswith(TOOL_ERROR_PREFIX))
        self.assertTrue(execute_tool("compact_context", {"extra": True},
                                     request_compaction=callback).startswith(TOOL_ERROR_PREFIX))
        callback.assert_not_called()
        self.assertIn("运行时", execute_tool("compact_context", {}))

        def call(call_id, name="compact_context", arguments=None):
            return {"id": call_id, "type": "function", "function": {
                "name": name, "arguments": json.dumps(arguments or {}),
            }}

        original = [
            {"role": "user", "content": "目标整理只读报告，保留 T-31。" + "背景材料。" * 700},
            {"role": "assistant", "content": "已记录目标，尚未执行写入。"},
        ]
        from codex.verify_session_memory import memory
        summary = memory()
        summary["episode_memory"]["task_description"] = "整理只读报告 T-31。"
        requests = []

        def model(messages, *definitions, on_usage=None, **options):
            requests.append(copy.deepcopy(messages))
            on_usage({"prompt_tokens": 900, "completion_tokens": 100, "total_tokens": 1000})
            number = len(requests)
            if number in (3, 6):
                self.assertEqual(definitions, ())
                self.assertEqual(options["max_tokens"], 1024)
                # 摘要出现前，当前任务已经有最终回答并保存到磁盘。
                current = Session("compact_tool").load()
                self.assertEqual(current[-1]["role"], "assistant")
                self.assertTrue(current[-1]["content"].startswith("本轮任务完成"))
                return {"role": "assistant", "content": "坏摘要" if number == 3 else json.dumps(summary, ensure_ascii=False)}
            self.assertIn("compact_context", [tool["name"] for tool in definitions[0]])
            if number == 1:
                return {"role": "assistant", "tool_calls": [call("first_request")]}
            if number == 4:
                return {"role": "assistant", "tool_calls": [
                    call("second_request"), call("read", "read_file", {"file_path": "source.txt"}), call("duplicate"),
                ]}
            results = [m for m in messages if m["role"] == "tool"]
            if number == 2:
                self.assertIn("已登记", results[-1]["content"])
            else:
                self.assertEqual([m["tool_call_id"] for m in results[-3:]], ["second_request", "read", "duplicate"])
                self.assertIn("真实样例", results[-2]["content"])
                self.assertIn("已经登记", results[-1]["content"])
            return {"role": "assistant", "content": f"本轮任务完成 {number}。"}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "source.txt").write_text("真实样例", encoding="utf-8")
            output, errors = io.StringIO(), io.StringIO()
            with (
                patch("personal_agent.tools.PROJECT_ROOT", root),
                patch("personal_agent.tool_output.PROJECT_ROOT", root),
                patch("personal_agent.session.PROJECT_ROOT", root),
                patch("personal_agent.main.get_model_message", side_effect=model) as request,
                redirect_stdout(output), redirect_stderr(errors),
            ):
                session = Session("compact_tool", with_tools=True)
                session.create()
                session.save(original, plan=PlanSession("plan").snapshot())

                def inputs():
                    yield "登记折叠，继续完成本轮任务"
                    first = session.load()
                    self.assertEqual(first[:len(original)], original)
                    self.assertEqual(first[-1]["content"], "本轮任务完成 2。")
                    yield "再次登记，并读取 source.txt"
                    saved = Session("compact_tool")
                    compacted = saved.load()
                    self.assertTrue(compacted[0]["content"].startswith(SUMMARY_PREFIX))
                    self.assertEqual(compacted[2]["content"], "再次登记，并读取 source.txt")
                    self.assertEqual(saved.plan_snapshot["state"], "planning")
                    self.assertEqual(request.call_count, 6)  # 重复登记没有多生成一次摘要。
                    yield "预算已用完，继续提问"
                    self.assertEqual(session.load(), compacted)
                    yield "/usage"
                    yield "exit"

                with patch("builtins.input", side_effect=inputs()):
                    history = run_repl(max_turns=2, session_name="compact_tool", resume=True,
                                       prices=TokenPrices(Decimal(1), Decimal(1)), max_cost=Decimal("0.006"))
                self.assertEqual(session.load(), history)
        self.assertEqual(len(requests), 6)
        self.assertIn("工具登记的折叠失败", errors.getvalue())
        self.assertIn("已达到本次运行估算费用预算", errors.getvalue())
        self.assertIn("逻辑调用 6 次", output.getvalue())
