"""一个主流程验证索引自动刷新、同名更新和手动重建。"""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from personal_agent.main import run_repl
from personal_agent.memory import list_memories, load_memory_index, read_memory, save_memory


class MemoryIndexSmokeTest(unittest.TestCase):
    def test_save_update_and_rebuild_index_without_model_calls(self):
        with tempfile.TemporaryDirectory() as temporary, patch("personal_agent.memory.PROJECT_ROOT", Path(temporary).resolve()):
            path = save_memory("编码习惯", "完整正文仍要保留。", description="旧摘要")
            self.assertIn("旧摘要", load_memory_index())
            save_memory("编码习惯", "完整正文仍要保留。", description="统一使用 UTF-8")
            save_memory("验证方式", "只检查主流程。\n避免大量回归。")
            index = load_memory_index()
            self.assertNotIn("旧摘要", index)
            self.assertEqual(index.count("[编码习惯]"), 1)
            self.assertIn(f"({path.name}) [project] — 统一使用 UTF-8", index)
            self.assertIn("只检查主流程。 避免大量回归。", index)
            self.assertEqual(len(list_memories()), 2)  # MEMORY.md 不作为记忆条目。
            self.assertEqual(read_memory("编码习惯")["content"], "完整正文仍要保留。")
            (path.parent / "MEMORY.md").write_text("过期索引", encoding="utf-8")
            output = io.StringIO()
            with patch("personal_agent.main.get_model_message") as model, patch("builtins.input", side_effect=["/memory index", "exit"]), redirect_stdout(output):
                self.assertEqual(run_repl(), [])
                model.assert_not_called()
            self.assertNotIn("过期索引", load_memory_index())
            self.assertIn("统一使用 UTF-8", output.getvalue())
