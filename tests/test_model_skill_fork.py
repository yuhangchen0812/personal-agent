"""模型 fork 两条主流程：主线程屏障与隔离，以及权限/预算/取消。"""

import copy
import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from personal_agent.main import run_repl
from personal_agent.permissions import READ_TOOLS
from personal_agent.session import Session
from personal_agent.subagent import GENERAL_TOOLS
from personal_agent.tool_registry import TOOL_REGISTRY, execute_tool
from personal_agent.tools import read_file, write_file
from personal_agent.usage import TokenPrices


def call(call_id, name, arguments):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


class ModelForkSkillSmokeTests(unittest.TestCase):
    def test_fork_is_a_main_thread_barrier_and_only_returns_its_conclusion(self):
        self.assertNotIn("skill", READ_TOOLS)
        main_thread = threading.get_ident()
        gate, lock = threading.Barrier(2, timeout=5), threading.Lock()
        active = 0
        observations, approvals = [], []
        counts = {"parent": 0, "child": 0}
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            root, user_root = base / "project", base / "home/.personal/skills"
            project = root / ".personal/skills/review/SKILL.md"
            project.parent.mkdir(parents=True)
            project.write_text("---\nname: review\n---\nPROJECT_METHOD_MUST_NOT_LOAD", encoding="utf-8")
            user = user_root / "review/SKILL.md"
            user.parent.mkdir(parents=True)
            user.write_text("---\nname: review\nuser-invocable: false\ncontext: fork\nallowed-tools: read_file, write_file, grep_search, skill\n---\nPRIVATE_FORK_METHOD $ARGUMENTS\n目录：${CLAUDE_SKILL_DIR}", encoding="utf-8")
            inline = root / ".personal/skills/inline-method/SKILL.md"
            inline.parent.mkdir()
            inline.write_text("---\nuser-invocable: false\n---\nINLINE_CHILD_METHOD $ARGUMENTS", encoding="utf-8")
            state = root / "playground/state.txt"
            state.parent.mkdir()
            state.write_text("旧内容", encoding="utf-8")
            (root / "source.txt").write_text("CHILD_FILE_EVIDENCE", encoding="utf-8")
            arguments = r"source.txt C:\new\1.txt $ARGUMENTS"

            def read(**arguments):
                nonlocal active
                if arguments["file_path"] != "playground/state.txt":
                    return read_file(**arguments)
                with lock:
                    active += 1
                try:
                    gate.wait()
                    result = read_file(**arguments)
                    observations.append((threading.get_ident(), result))
                    return result
                finally:
                    with lock:
                        active -= 1

            def approve(name, *_):
                self.assertEqual(threading.get_ident(), main_thread)
                self.assertEqual(active, 0)
                self.assertEqual(len(observations), 2)
                approvals.append(name)
                return True

            def model(messages, definitions, *, on_usage):
                self.assertEqual(threading.get_ident(), main_thread)
                child = "skill-fork" in messages[0]["content"]
                role = "child" if child else "parent"
                counts[role] += 1
                on_usage({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                if not child:
                    self.assertNotIn("PRIVATE_FORK_METHOD", str(messages))
                    self.assertNotIn("INLINE_CHILD_METHOD", str(messages))
                    self.assertNotIn("PROJECT_METHOD_MUST_NOT_LOAD", str(messages))
                    self.assertIn("PARENT_MEMORY", str(messages))
                    if counts[role] == 1:
                        return {"role": "assistant", "tool_calls": [
                            call("before_a", "read_file", {"file_path": "playground/state.txt"}),
                            call("before_b", "read_file", {"file_path": "playground/state.txt"}),
                            call("fork_task", "skill", {"skill_name": "review", "args": arguments}),
                            call("after_a", "read_file", {"file_path": "playground/state.txt"}),
                            call("after_b", "read_file", {"file_path": "playground/state.txt"}),
                        ]}
                    results = [item for item in messages if item["role"] == "tool"]
                    self.assertEqual([item["tool_call_id"] for item in results], ["before_a", "before_b", "fork_task", "after_a", "after_b"])
                    self.assertTrue(all("旧内容" in item["content"] for item in results[:2]))
                    self.assertTrue(all("新内容" in item["content"] for item in results[3:]))
                    self.assertIn("fork 技能已返回：review", results[2]["content"])
                    self.assertIn("技能作用域：user", results[2]["content"])
                    self.assertIn("CHILD_CONCLUSION", results[2]["content"])
                    self.assertNotIn("child_read", str(messages))
                    return {"role": "assistant", "content": "父模型依据子结论完成回答"}
                self.assertNotIn("PRIVATE_PARENT", str(messages))
                self.assertNotIn("PARENT_MEMORY", str(messages))
                self.assertIn(f"PRIVATE_FORK_METHOD {arguments}", messages[0]["content"])
                self.assertIn(f"目录：{user.parent}", messages[0]["content"])
                if counts[role] == 1:
                    self.assertEqual(active, 0)
                    self.assertEqual(len(observations), 2)
                    self.assertEqual([item["role"] for item in messages], ["system", "user"])
                    self.assertEqual(messages[1]["content"], arguments)
                    self.assertIn("只加载 inline", next(item for item in definitions if item["name"] == "skill")["description"])
                    user.write_text("---\ncontext: fork\nallowed-tools: []\n---\nCHANGED_METHOD", encoding="utf-8")
                    return {"role": "assistant", "tool_calls": [
                        call("child_read", "read_file", {"file_path": "source.txt"}),
                        call("inline", "skill", {"skill_name": "inline-method", "args": "source.txt"}),
                        call("recursive", "skill", {"skill_name": "review"}),
                        call("write", "write_file", {"file_path": "playground/state.txt", "content": "新内容"}),
                        call("denied", "grep_search", {"pattern": "EVIDENCE"}),
                        call("control", "compact_context", {}),
                    ]}
                results = {item["tool_call_id"]: item["content"] for item in messages if item["role"] == "tool"}
                self.assertIn("CHILD_FILE_EVIDENCE", results["child_read"])
                self.assertIn("INLINE_CHILD_METHOD source.txt", results["inline"])
                self.assertIn("禁止递归 fork", results["recursive"])
                self.assertIn("已覆盖", results["write"])
                self.assertIn("显式 deny", results["denied"])
                self.assertIn("skill-fork:review 仅允许", results["control"])
                return {"role": "assistant", "content": "CHILD_CONCLUSION"}

            output, errors = io.StringIO(), io.StringIO()
            writer, blocked = Mock(wraps=write_file), Mock()
            with (
                patch("personal_agent.skills.PROJECT_ROOT", root),
                patch("personal_agent.skills.USER_SKILLS_ROOT", user_root),
                patch("personal_agent.agent_config.PROJECT_ROOT", root),
                patch("personal_agent.agent_config.USER_AGENTS_ROOT", base / "no-user-agents"),
                patch("personal_agent.tools.PROJECT_ROOT", root),
                patch("personal_agent.memory.PROJECT_ROOT", root),
                patch("personal_agent.session.PROJECT_ROOT", root),
                patch.dict(TOOL_REGISTRY["read_file"], {"handler": read}),
                patch.dict(TOOL_REGISTRY["write_file"], {"handler": writer}),
                patch.dict(TOOL_REGISTRY["grep_search"], {"handler": blocked}),
                patch("personal_agent.main.get_model_message", side_effect=model) as request,
                patch("personal_agent.main.confirm_in_terminal", side_effect=approve),
                redirect_stdout(output), redirect_stderr(errors),
            ):
                session = Session("model_fork", with_tools=True)
                session.create()
                session.save([{"role": "user", "content": "PRIVATE_PARENT"}, {"role": "assistant", "content": "已有回答"}])
                with patch("builtins.input", side_effect=["/memory save profile PARENT_MEMORY", "/memory use profile", "调查并使用 review 技能", "/usage", "exit"]):
                    history = run_repl(session_name="model_fork", resume=True, deny=["grep_search"])
                self.assertEqual(session.load(), history)
                with patch("builtins.input", side_effect=["exit"]):
                    self.assertEqual(run_repl(session_name="model_fork", resume=True), history)
                self.assertEqual(request.call_count, 4)
            self.assertEqual(counts, {"parent": 2, "child": 2})
            self.assertEqual(approvals, ["write_file"])
            self.assertEqual(len(observations), 4)
            self.assertTrue(all(thread != main_thread for thread, _ in observations))
            self.assertEqual(state.read_text(encoding="utf-8"), "新内容")
            self.assertNotIn("PRIVATE_FORK_METHOD", str(history))
            self.assertNotIn("child_read", str(history))
            self.assertIn("逻辑调用 4 次，完整用量 4 次", output.getvalue())
            self.assertEqual(errors.getvalue(), "")
            writer.assert_called_once()
            blocked.assert_not_called()

    def test_model_fork_respects_deny_plan_budget_empty_tools_and_cancellation(self):
        handler, delegate = Mock(), Mock()
        with patch.dict(TOOL_REGISTRY["skill"], {"handler": handler}):
            for arguments, options in [
                ({"skill_name": "review"}, {"deny": ["skill"]}),
                ({"skill_name": "review", "fork_skill": True}, {}),
            ]:
                self.assertIn("工具调用失败", execute_tool("skill", arguments, fork_skill=delegate, **options))
        handler.assert_not_called()
        delegate.assert_not_called()
        for scenario in ("plan", "budget", "empty", "cancel"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                path = root / ".personal/skills/review/SKILL.md"
                path.parent.mkdir(parents=True)
                declaration = "allowed-tools: []\n" if scenario == "empty" else ""
                path.write_text(f"---\ncontext: fork\n{declaration}---\nGUARDED_FORK_METHOD", encoding="utf-8")
                counts = {"parent": 0, "child": 0}

                def model(messages, definitions, *, on_usage):
                    child = "skill-fork" in messages[0]["content"]
                    role = "child" if child else "parent"
                    counts[role] += 1
                    on_usage({"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1})
                    if not child:
                        if counts[role] == 1:
                            calls = [call("fork", "skill", {"skill_name": "review"})]
                            if scenario == "cancel":
                                calls.append(call("later_write", "write_file", {"file_path": "playground/result.txt", "content": "不应执行"}))
                            return {"role": "assistant", "tool_calls": calls}
                        self.assertIn("fork 技能已返回", messages[-1]["content"])
                        return {"role": "assistant", "content": "父任务结束"}
                    if scenario == "cancel":
                        raise KeyboardInterrupt()
                    if counts[role] == 1:
                        self.assertEqual({item["name"] for item in definitions}, set() if scenario == "empty" else GENERAL_TOOLS)
                        return {"role": "assistant", "tool_calls": [call("write", "write_file", {"file_path": "playground/result.txt", "content": "不应执行"})]}
                    self.assertIn("仅允许" if scenario == "empty" else "权限拒绝", messages[-1]["content"])
                    return {"role": "assistant", "content": "子任务如实报告限制"}

                output, errors = io.StringIO(), io.StringIO()
                blocked = Mock()
                with (
                    patch("personal_agent.skills.PROJECT_ROOT", root),
                    patch("personal_agent.skills.USER_SKILLS_ROOT", root / "no-user-skills"),
                    patch("personal_agent.agent_config.PROJECT_ROOT", root),
                    patch("personal_agent.agent_config.USER_AGENTS_ROOT", root / "no-user-agents"),
                    patch("personal_agent.session.PROJECT_ROOT", root),
                    patch.dict(TOOL_REGISTRY["write_file"], {"handler": blocked}),
                    patch("personal_agent.main.get_model_message", side_effect=model),
                    patch("personal_agent.main.confirm_in_terminal") as confirm,
                    patch("builtins.input", side_effect=["使用 review 技能", "/usage", "exit"]),
                    redirect_stdout(output), redirect_stderr(errors),
                ):
                    history = run_repl(
                        session_name="fork_guards", with_tools=True,
                        permission_mode="plan" if scenario == "plan" else "default",
                        allow=["write_file"] if scenario == "plan" else None,
                        prices=TokenPrices(Decimal(1_000_000), Decimal(1_000_000)),
                        max_cost=Decimal(2 if scenario == "budget" else 10),
                    )
                    self.assertEqual(Session("fork_guards").load(), history)
                if scenario in ("plan", "empty"):
                    self.assertEqual(counts, {"parent": 2, "child": 2})
                    self.assertEqual(len(history), 4)
                    self.assertEqual(errors.getvalue(), "")
                else:
                    self.assertEqual(counts, {"parent": 1, "child": 1})
                    self.assertEqual(history, [])
                if scenario == "budget":
                    self.assertIn("已达到本次运行估算费用预算", errors.getvalue())
                    self.assertIn("逻辑调用 2 次，完整用量 2 次", output.getvalue())
                elif scenario == "cancel":
                    self.assertIn("已中断本轮任务", output.getvalue())
                blocked.assert_not_called()
                confirm.assert_not_called()
