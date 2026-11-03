"""文本会话的三个核心测试；文件真实读写，模型被替换。"""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from personal_agent.main import main, run_repl
from personal_agent.session import TextSession


class TextSessionTests(unittest.TestCase):
    def test_roundtrip_and_failed_replacement_preserve_saved_bytes(self):
        with tempfile.TemporaryDirectory() as temporary, patch("personal_agent.session.PROJECT_ROOT", Path(temporary)):
            session = TextSession("study")
            self.assertEqual(session.create(), [])
            messages = [{"role": "user", "content": "记住：松果-8624。"}, {"role": "assistant", "content": "已记住。"}]
            session.save(messages)
            self.assertEqual(TextSession("study").load(), messages)
            saved = session.path.read_bytes()
            self.assertIn("松果-8624", saved.decode("utf-8"))
            with self.assertRaisesRegex(ValueError, "已存在"):
                TextSession("study").create()
            longer = messages + [{"role": "user", "content": "下一问"}, {"role": "assistant", "content": "下一答"}]
            with patch("personal_agent.session.os.replace", side_effect=OSError("模拟替换失败")):
                with self.assertRaises(OSError):
                    session.save(longer)
            self.assertEqual(session.path.read_bytes(), saved)
            self.assertEqual(list(session.path.parent.glob("*.tmp")), [])
            with self.assertRaises(ValueError):
                session.save(messages + [{"role": "user", "content": "尚未回答"}])
            self.assertEqual(session.path.read_bytes(), saved)
            messages[0]["content"] = "只修改内存"
            self.assertEqual(session.load()[0]["content"], "记住：松果-8624。")

    def test_invalid_or_other_project_session_fails_before_model_request(self):
        with tempfile.TemporaryDirectory() as temporary, patch("personal_agent.session.PROJECT_ROOT", Path(temporary)):
            session = TextSession("study")
            session.create()
            original = json.loads(session.path.read_text(encoding="utf-8"))
            other_project = {**original, "project_root": str(Path(temporary) / "other")}
            incomplete = {**original, "messages": [{"role": "user", "content": "半轮"}]}
            tools = {**original, "messages": [{"role": "user", "content": "请求工具"}, {"role": "assistant", "content": "正文", "tool_calls": []}]}
            invalid_documents = ["{", json.dumps(other_project), json.dumps({**original, "project_root": ""}), json.dumps(incomplete), json.dumps(tools), json.dumps({**original, "version": 99})]
            for document in invalid_documents:
                with self.subTest(document=document):
                    session.path.write_text(document, encoding="utf-8")
                    saved = session.path.read_bytes()
                    with patch("personal_agent.main.get_model_message") as model, redirect_stderr(io.StringIO()):
                        with self.assertRaises(SystemExit) as result:
                            main(["--resume", "study"])
                    self.assertEqual(result.exception.code, 1)
                    model.assert_not_called()
                    self.assertEqual(session.path.read_bytes(), saved)
            with self.assertRaises(ValueError):
                TextSession("../outside")
            with self.assertRaisesRegex(ValueError, "找不到会话"):
                TextSession("missing").load()
            with self.assertRaisesRegex(ValueError, "Plan"):
                run_repl(session_name="new", permission_mode="plan")
            self.assertFalse(TextSession("new").path.exists())

    def test_repl_saves_complete_turns_restores_and_handles_save_failure(self):
        with tempfile.TemporaryDirectory() as temporary, patch("personal_agent.session.PROJECT_ROOT", Path(temporary)):
            requests = []
            responses = iter([
                {"role": "assistant", "content": "已记住口令。"},
                RuntimeError("模拟请求失败"), KeyboardInterrupt(),
                {"role": "assistant", "content": None, "tool_calls": [{"id": "blocked", "type": "function", "function": {"name": "run_shell", "arguments": "{}"}}]},
            ])

            def model(messages, definitions=None, **_):
                self.assertIsNone(definitions)
                requests.append(copy.deepcopy(messages))
                result = next(responses)
                if isinstance(result, BaseException):
                    raise result
                return result

            output, errors = io.StringIO(), io.StringIO()
            with (
                patch("builtins.input", side_effect=["记住口令：松果-8624", "失败的问题", "被中断的问题", "意外的工具响应", "exit"]),
                patch("personal_agent.main.get_model_message", side_effect=model),
                patch("personal_agent.main.execute_tool") as execute,
                redirect_stdout(output), redirect_stderr(errors),
            ):
                first = run_repl(session_name="study")
            execute.assert_not_called()
            session = TextSession("study")
            self.assertEqual(session.load(), first)
            self.assertEqual(len(first), 2)
            self.assertIn("不支持工具调用", errors.getvalue())

            resumed_requests = []
            def resumed_model(messages, definitions=None, **_):
                self.assertIsNone(definitions)
                resumed_requests.append(copy.deepcopy(messages))
                return {"role": "assistant", "content": "松果-8624"}

            with (
                patch("builtins.input", side_effect=["刚才的口令是什么？", "exit"]),
                patch("personal_agent.main.get_model_message", side_effect=resumed_model),
                redirect_stdout(output), redirect_stderr(errors),
            ):
                second = run_repl(session_name="study", resume=True)
            self.assertEqual(resumed_requests, [first + [{"role": "user", "content": "刚才的口令是什么？"}]])
            self.assertEqual(session.load(), second)
            self.assertEqual(len(second), 4)
            saved = session.path.read_bytes()

            with (
                patch("builtins.input", side_effect=["再问一次", "exit"]),
                patch("personal_agent.main.get_model_message", return_value={"role": "assistant", "content": "仍然记得。"}),
                patch("personal_agent.session.os.replace", side_effect=OSError("模拟磁盘失败")),
                redirect_stdout(output), redirect_stderr(errors),
            ):
                in_memory = run_repl(session_name="study", resume=True)
            self.assertEqual(len(in_memory), 6)
            self.assertEqual(session.path.read_bytes(), saved)
            self.assertIn("磁盘存档未更新", errors.getvalue())
