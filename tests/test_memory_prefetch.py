"""一个主流程检查提前读取索引、缓存复用和保存/外部修改后的刷新。"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from personal_agent.main import run_repl
from personal_agent.memory import load_memory_index, read_memory, save_memory


class MemoryPrefetchSmokeTest(unittest.TestCase):
    def test_prefetch_reuses_index_and_refreshes_after_changes(self):
        requests = []

        def model(messages, *_, **__):
            question = [message for message in messages if message["role"] == "user"][-1]["content"]
            payload = question.split("<project_memory>\n", 1)[1].split("\n</project_memory>", 1)[0]
            requests.append(json.loads(payload))
            return {"role": "assistant", "content": "固定回答。"}

        with tempfile.TemporaryDirectory() as temporary, patch("personal_agent.memory.PROJECT_ROOT", Path(temporary).resolve()):
            path = save_memory("编码习惯", "旧约定：使用 UTF-8。")
            index = path.parent / "MEMORY.md"

            def inputs():
                yield "/memory auto"
                self.assertEqual(load.call_count, 1)  # 提问前已读取索引，未预读正文。
                read.assert_not_called()
                yield "编码习惯是什么？"
                self.assertEqual((load.call_count, read.call_count), (1, 1))
                yield "再说一下编码习惯？"
                self.assertEqual((load.call_count, read.call_count), (1, 2))
                yield "/memory save 编码习惯 新约定：明确指定 UTF-8。"
                yield "编码习惯是什么？"
                self.assertEqual(load.call_count, 2)
                index.write_text(index.read_text(encoding="utf-8") + "\n", encoding="utf-8")
                yield "编码习惯是什么？"
                self.assertEqual(load.call_count, 3)  # 外部修改后的下一轮重新读取。
                yield "/memory off"
                yield "exit"

            with (
                patch("personal_agent.memory.load_memory_index", wraps=load_memory_index) as load,
                patch("personal_agent.memory.read_memory", wraps=read_memory) as read,
                patch("personal_agent.main.get_model_message", side_effect=model),
                patch("builtins.input", side_effect=inputs()), redirect_stdout(io.StringIO()),
            ):
                history = run_repl()

        self.assertEqual(len(requests), 4)  # 预取与本地命令没有增加模型调用。
        self.assertEqual([entry["content"] for entry in requests], [
            "旧约定：使用 UTF-8。", "旧约定：使用 UTF-8。",
            "新约定：明确指定 UTF-8。", "新约定：明确指定 UTF-8。",
        ])
        self.assertTrue(all("<project_memory>" not in message["content"] for message in history))
