"""一条折叠主流程检查结构摘要、失败保留、工具配对、Plan 和预算。"""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from personal_agent.context import SUMMARY_PREFIX
from personal_agent.main import run_repl
from personal_agent.plan import PlanSession
from personal_agent.session import Session
from personal_agent.session_memory import SessionMemoryStore
from personal_agent.usage import TokenPrices


class CompactSmokeTest(unittest.TestCase):
    def test_compact_commits_only_after_validation_and_save_and_keeps_budget(self):
        def call(call_id):
            return {"role": "assistant", "tool_calls": [{"id": call_id, "type": "function", "function": {
                "name": "read_file", "arguments": '{"file_path":"source.txt"}',
            }}]}

        recent = [
            {"role": "user", "content": "最近一轮：读取 source.txt，仍禁止任务写入。"},
            call("recent_read"),
            {"role": "tool", "tool_call_id": "recent_read", "content": "   1 | 已核实的样例内容。"},
            {"role": "assistant", "content": "最近一轮读取完成。"},
        ]
        original = [
            {"role": "user", "content": "目标：整理报告；约束：禁止任务写入，保留标识 R-17。" + "背景材料。" * 600},
            call("old_read"),
            {"role": "tool", "tool_call_id": "old_read", "content": "   1 | 已核实的样例内容。"},
            {"role": "assistant", "content": "已读取 source.txt，尚未生成报告。"},
            {"role": "user", "content": "继续保持只读，等待批准。" + "历史补充。" * 300},
            {"role": "assistant", "content": "下一步整理提纲，不执行写入。"},
            *recent,
        ]
        from codex.verify_session_memory import memory
        summary = memory()
        summary["episode_memory"]["task_description"] = "整理报告，保留标识 R-17。"
        requests = []

        def model(messages, *definitions, on_usage=None, **options):
            requests.append(copy.deepcopy(messages))
            on_usage({"prompt_tokens": 900, "completion_tokens": 100, "total_tokens": 1000})
            if len(requests) <= 3:
                self.assertEqual(definitions, ())  # 摘要请求没有工具定义。
                self.assertEqual(options["max_tokens"], 1024)
                self.assertEqual(json.loads(messages[1]["content"])["messages"], original[:-len(recent)])
                return {"role": "assistant", "content": "无效 JSON" if len(requests) == 1 else json.dumps(summary, ensure_ascii=False)}
            self.assertTrue(any(m["role"] == "system" and "planning" in m["content"] for m in messages))
            self.assertTrue(any(m["role"] == "user" and m["content"].startswith(SUMMARY_PREFIX) for m in messages))
            self.assertEqual([m["tool_call_id"] for m in messages if m["role"] == "tool"], ["recent_read"])
            return {"role": "assistant", "content": "继续遵守只读约束，先整理提纲。"}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            output, errors = io.StringIO(), io.StringIO()
            with (
                patch("personal_agent.tool_output.PROJECT_ROOT", root),
                patch("personal_agent.tools.PROJECT_ROOT", root),
                patch("personal_agent.session.PROJECT_ROOT", root),
                patch("personal_agent.main.get_model_message", side_effect=model) as request,
                redirect_stdout(output), redirect_stderr(errors),
            ):
                plan = PlanSession("plan")
                session = Session("compact_demo", with_tools=True)
                session.create()
                session.save(original, plan=plan.snapshot())

                def inputs():
                    yield "/compact"  # 无效摘要：原磁盘和内存历史保留。
                    self.assertEqual(Session("compact_demo").load(), original)
                    with patch("personal_agent.session.Session.save", side_effect=OSError("模拟存档失败")):
                        yield "/compact"
                    self.assertEqual(Session("compact_demo").load(), original)
                    yield "/compact"
                    saved = Session("compact_demo")
                    compacted = saved.load()
                    self.assertEqual(compacted[-len(recent):], recent)
                    self.assertEqual(saved.plan_snapshot["state"], "planning")
                    self.assertTrue(compacted[0]["content"].startswith(SUMMARY_PREFIX))
                    record = SessionMemoryStore(saved).read(saved.session_memory)
                    self.assertEqual(record["memory"]["episode_memory"]["task_description"], "整理报告，保留标识 R-17。")
                    backup = record["source_archive"]
                    self.assertEqual(json.loads((root / backup).read_text(encoding="utf-8")), original)
                    yield "/compact"  # 没有新增旧轮次，不再次请求模型。
                    self.assertEqual(request.call_count, 3)
                    yield "继续，但仍不可写入"
                    before_block = Session("compact_demo").load()
                    yield "/compact"  # 预算已用完：不请求模型，不改变历史。
                    self.assertEqual(Session("compact_demo").load(), before_block)
                    yield "/usage"
                    yield "exit"

                with patch("builtins.input", side_effect=inputs()):
                    history = run_repl(session_name="compact_demo", resume=True,
                                       prices=TokenPrices(Decimal(1), Decimal(1)), max_cost=Decimal("0.004"))
                self.assertEqual(Session("compact_demo").load(), history)
        self.assertEqual(len(requests), 4)
        self.assertIn("不是有效 JSON", errors.getvalue())
        self.assertIn("模拟存档失败", errors.getvalue())
        self.assertIn("已达到本次运行估算费用预算", errors.getvalue())
        self.assertIn("逻辑调用 4 次", output.getvalue())
        self.assertIn("上下文已折叠：JSON 字符数", output.getvalue())
