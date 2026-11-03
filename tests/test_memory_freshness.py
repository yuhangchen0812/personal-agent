"""一个主流程检查旧记忆时间、手动快照和自动重新读取。"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from personal_agent.main import run_repl
from personal_agent.memory import save_memory


class MemoryFreshnessSmokeTest(unittest.TestCase):
    def test_timestamps_and_hints_follow_the_selected_snapshot(self):
        requests = []
        now = datetime(2026, 10, 27, tzinfo=timezone.utc)
        old = now - timedelta(days=45)
        recent = now - timedelta(hours=1)

        def model(messages, *_, **__):
            question = [message for message in messages if message["role"] == "user"][-1]["content"]
            payload = question.split("<project_memory>\n", 1)[1].split("\n</project_memory>", 1)[0]
            requests.append(json.loads(payload))
            return {"role": "assistant", "content": "固定回答。"}

        with tempfile.TemporaryDirectory() as temporary, patch("personal_agent.memory.PROJECT_ROOT", Path(temporary).resolve()):
            path = save_memory("费用说明", "旧费用：每次 1 元。")
            os.utime(path, (old.timestamp(), old.timestamp()))

            def inputs():
                yield "/memory"
                yield "/memory read 费用说明"
                yield "/memory use 费用说明"
                yield "费用说明是什么？"
                updated_path = save_memory("费用说明", "新费用：每次 2 元。")
                os.utime(updated_path, (recent.timestamp(), recent.timestamp()))
                yield "费用说明是什么？"  # 手动模式仍使用已选用的旧正文和旧时间。
                yield "/memory auto"
                yield "费用说明是什么？"  # 自动模式读取新正文和新时间。
                yield "exit"

            output = io.StringIO()
            with (
                patch("personal_agent.memory.datetime", wraps=datetime) as clock,
                patch("personal_agent.main.get_model_message", side_effect=model),
                patch("builtins.input", side_effect=inputs()), redirect_stdout(output),
            ):
                clock.now.return_value = now
                history = run_repl()

            self.assertEqual(len(requests), 3)
            self.assertEqual([entry["updated_at"] for entry in requests], [old.isoformat(), old.isoformat(), recent.isoformat()])
            self.assertEqual([entry["content"] for entry in requests], ["旧费用：每次 1 元。", "旧费用：每次 1 元。", "新费用：每次 2 元。"])
            self.assertIn("距今 45 天", requests[0]["freshness_hint"])
            self.assertIn("距今不足 1 天", requests[2]["freshness_hint"])
            self.assertTrue(all("不代表内容已核实" in entry["freshness_hint"] for entry in requests))
            self.assertIn(old.isoformat(), output.getvalue())
            self.assertIn("距今 45 天", output.getvalue())
            self.assertNotIn("updated_at:", path.read_text(encoding="utf-8"))  # 原三字段文件仍兼容。
            self.assertTrue(all("<project_memory>" not in message["content"] for message in history))
