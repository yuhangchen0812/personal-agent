"""定向验证只读并发、消息顺序和写入/权限屏障，不请求真实模型。"""

import copy
import io
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from personal_agent.agent import run_agent_loop
from personal_agent.main import run_repl
from personal_agent.permissions import READ_TOOLS
from personal_agent.plan import PlanSession
from personal_agent.session import Session
from personal_agent.tool_registry import TOOL_REGISTRY, execute_tool
from test_agent import scripted_model, tool_call


class ParallelReadTests(unittest.TestCase):
    def test_bounded_reads_finish_out_of_order_but_save_in_request_order(self):
        main_thread = threading.get_ident()
        lock = threading.Lock()
        started = threading.Barrier(3, timeout=5)
        finished = {name: threading.Event() for name in ("list_files", "grep_search")}
        active = peak = 0
        completed = []
        saves = []
        original_save = Session.save
        calls = [
            tool_call("slow", "read_file", {"file_path": "source.txt"}),
            tool_call("denied", "list_files", {"pattern": "*.txt"}),
            tool_call("search", "grep_search", {"pattern": "中文", "include": "*.txt"}),
            tool_call("error", "read_file", {"file_path": "raise.txt"}),
        ]

        def dispatch(name, arguments, **options):
            nonlocal active, peak
            label = arguments.get("file_path", name)
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                if label != "raise.txt":
                    started.wait()  # 三个调用必须同时在执行；不是靠总耗时猜测并发。
                if label == "source.txt":
                    for event in finished.values():
                        if not event.wait(5):
                            raise RuntimeError("其他读取未完成")
                if label == "raise.txt":
                    raise RuntimeError("异常正文不应泄露到工具结果")
                return execute_tool(name, arguments, **options)
            finally:
                with lock:
                    active -= 1
                    completed.append(label)
                if label in finished:
                    finished[label].set()

        def save(session, *args, **kwargs):
            saves.append(threading.get_ident())
            return original_save(session, *args, **kwargs)

        requests = []
        model = scripted_model([
            {"role": "assistant", "tool_calls": calls},
            {"role": "assistant", "content": "读取完成，拒绝和异常均已记录。"},
        ], requests)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "source.txt").write_text("真实中文内容\n", encoding="utf-8")
            with (
                patch("personal_agent.tools.PROJECT_ROOT", root),
                patch("personal_agent.session.PROJECT_ROOT", root),
                patch("personal_agent.main.get_model_message", side_effect=lambda messages, definitions, **_: model(messages, definitions)),
                patch("personal_agent.main.execute_tool", side_effect=dispatch),
                patch.object(Session, "save", autospec=True, side_effect=save),
                patch("builtins.input", side_effect=["同时调查项目", "exit"]),
                redirect_stdout(io.StringIO()),
            ):
                history = run_repl(session_name="parallel", with_tools=True, permission_mode="plan", deny=["list_files"])
                session = Session("parallel")
                self.assertEqual(session.load(), history)
                self.assertEqual(session.plan_snapshot["state"], "planning")
        self.assertEqual(peak, 3)
        self.assertEqual(active, 0)
        self.assertLess(completed.index("list_files"), completed.index("source.txt"))
        self.assertLess(completed.index("grep_search"), completed.index("source.txt"))
        results = [message for message in requests[1] if message["role"] == "tool"]
        self.assertEqual([message["tool_call_id"] for message in results], ["slow", "denied", "search", "error"])
        self.assertIn("真实中文内容", results[0]["content"])
        self.assertIn("权限拒绝", results[1]["content"])
        self.assertIn("source.txt:1:真实中文内容", results[2]["content"])
        self.assertIn("RuntimeError", results[3]["content"])
        self.assertNotIn("异常正文", results[3]["content"])
        self.assertTrue(saves)
        self.assertEqual(set(saves), {main_thread})

    def test_writes_shell_and_plan_are_barriers_with_main_thread_approval(self):
        main_thread = threading.get_ident()
        gate = threading.Barrier(2, timeout=5)
        lock = threading.Lock()
        active = 0
        approvals = []
        observed = []
        plan = PlanSession()

        def approve(name, *_):
            self.assertEqual(threading.get_ident(), main_thread)
            self.assertEqual(active, 0)
            approvals.append(name)
            return True

        def dispatch(name, arguments):
            nonlocal active
            if name in READ_TOOLS:
                with lock:
                    active += 1
                try:
                    gate.wait()
                    return execute_tool(name, arguments, plan=plan, confirm=approve)
                finally:
                    with lock:
                        active -= 1
            self.assertEqual(threading.get_ident(), main_thread)
            self.assertEqual(active, 0)
            observed.append(name)
            return execute_tool(name, arguments, plan=plan, confirm=approve)

        calls = [
            tool_call("before1", "read_file", {"file_path": "playground/state.txt"}),
            tool_call("before2", "read_file", {"file_path": "playground/state.txt"}),
            tool_call("write", "write_file", {"file_path": "playground/state.txt", "content": "新内容"}),
            tool_call("after1", "read_file", {"file_path": "playground/state.txt"}),
            tool_call("after2", "read_file", {"file_path": "playground/state.txt"}),
            tool_call("shell", "run_shell", {"command": "固定模拟命令"}),
            tool_call("enter", "enter_plan_mode", {}),
            tool_call("blocked", "write_file", {"file_path": "playground/blocked.txt", "content": "不能写"}),
        ]
        shell = Mock(return_value="模拟 Shell 结果")
        requests = []
        messages = [{"role": "user", "content": "先读再写再读，最后进入规划"}]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "playground").mkdir()
            (root / "playground/state.txt").write_text("旧内容", encoding="utf-8")
            with patch("personal_agent.tools.PROJECT_ROOT", root), patch.dict(TOOL_REGISTRY["run_shell"], {"handler": shell}):
                run_agent_loop(messages, scripted_model([
                    {"role": "assistant", "tool_calls": calls},
                    {"role": "assistant", "content": "完成。"},
                ], requests), tool_executor=dispatch)
            self.assertEqual((root / "playground/state.txt").read_text(encoding="utf-8"), "新内容")
            self.assertFalse((root / "playground/blocked.txt").exists())
        results = {message["tool_call_id"]: message["content"] for message in requests[1] if message["role"] == "tool"}
        for call_id in ("before1", "before2"):
            self.assertIn("旧内容", results[call_id])
        for call_id in ("after1", "after2"):
            self.assertIn("新内容", results[call_id])
        self.assertEqual(observed, ["write_file", "run_shell", "enter_plan_mode", "write_file"])
        self.assertEqual(approvals, ["write_file", "run_shell"])
        self.assertIn("权限拒绝", results["blocked"])
        shell.assert_called_once()

    def test_explicit_serial_mode_and_invalid_parallelism(self):
        main_thread = threading.get_ident()
        observed = []

        def dispatch(name, arguments):
            observed.append((arguments["file_path"], threading.get_ident()))
            return "串行结果"

        messages = [{"role": "user", "content": "串行读取"}]
        replies = [
            {"role": "assistant", "tool_calls": [tool_call(str(index), "read_file", {"file_path": str(index)}) for index in range(2)]},
            {"role": "assistant", "content": "完成。"},
        ]
        run_agent_loop(messages, scripted_model(replies, []), tool_executor=dispatch, max_parallel_reads=1)
        self.assertEqual(observed, [("0", main_thread), ("1", main_thread)])
        for invalid in (0, True, "3"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "max_parallel_reads"):
                run_agent_loop(messages, Mock(), max_parallel_reads=invalid)


if __name__ == "__main__":
    unittest.main()
