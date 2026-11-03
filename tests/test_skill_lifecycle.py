"""两条主流程：运行时事件/整目录往返，以及冲突和持久化失败边界。"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from personal_agent.main import run_repl
from personal_agent.skill_lifecycle import SkillLifecycleStore
from personal_agent.skills import PreparedSkillCall, create_project_skill, discover_skills, load_skill
from personal_agent.subagent import run_fork_skill
from personal_agent.tool_registry import execute_tool


def tool_call(name, arguments, call_id):
    return {"role": "assistant", "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}


class SkillLifecycleSmokeTests(unittest.TestCase):
    def test_cli_events_archive_and_restore_preserve_directory_without_replaying_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch("personal_agent.skills.PROJECT_ROOT", root), patch("personal_agent.skills.USER_SKILLS_ROOT", root / "user-skills"), patch("personal_agent.session.PROJECT_ROOT", root):
                path = create_project_skill("review", "检查代码异常处理", "METHOD_INLINE $ARGUMENTS")
                branch = create_project_skill("branch", "分支任务", "METHOD_FORK $ARGUMENTS")
                branch.write_text(branch.read_text(encoding="utf-8").replace('context: "inline"', 'context: "fork"'), encoding="utf-8")
                resource = path.parent / "资料/示例.txt"
                resource.parent.mkdir()
                resource.write_bytes("资源原文\r\n".encode("utf-8"))
                original = {p.relative_to(path.parent).as_posix(): p.read_bytes() for p in path.parent.rglob("*") if p.is_file()}
                store = SkillLifecycleStore()
                requests = []

                def model(messages, definitions=None, *, on_usage, **options):
                    on_usage({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                    requests.append(messages)
                    if any("skill-fork:" in message.get("content", "") for message in messages if message["role"] == "system"):
                        return {"role": "assistant", "content": "分支已返回结论，未声明实际任务成功。"}
                    last = messages[-1]
                    if last["role"] == "tool":
                        return {"role": "assistant", "content": "已根据实际技能结果回答。"}
                    if last["content"].startswith("检查代码异常处理"):
                        return tool_call("skill", {"skill_name": "review", "args": "不写入事件的私有参数"}, "inline")
                    if last["content"].startswith("让模型委派"):
                        return tool_call("skill", {"skill_name": "branch"}, "fork")
                    return {"role": "assistant", "content": "手动 inline 回答。"}

                checkpoints = {}

                def inputs():
                    yield "/skills"
                    yield "/skills search 检查代码异常处理"
                    self.assertFalse((root / ".personal/skill-usage").exists())
                    yield "检查代码异常处理"
                    yield "/review 手动参数"
                    yield "/branch 手动 fork"
                    yield "让模型委派分支任务"
                    yield "/skill-stats"
                    rows = store.statistics("review")
                    self.assertEqual(rows[0]["counts"]["inline_loaded"], 2)
                    self.assertEqual(rows[0]["counts"]["retrieved"], 1)
                    self.assertEqual(store.statistics("branch")[0]["counts"]["fork_started"], 2)
                    self.assertEqual(store.statistics("branch")[0]["counts"]["fork_returned"], 2)
                    events = list((root / ".personal/skill-usage/events").glob("*.json"))
                    self.assertNotIn("不写入事件的私有参数", "".join(p.read_text(encoding="utf-8") for p in events))
                    count = len(events)
                    self.assertIn("权限拒绝", execute_tool("skill", {"skill_name": "review"}, deny=["skill"]))
                    self.assertEqual(len(list(events[0].parent.glob("*.json"))), count)
                    checkpoints["before_archive_requests"] = len(requests)
                    yield "/skill-archive review 暂时停用"
                    record = store.list_archives()[0]
                    archive_id = record["id"]
                    payload = store._archive_path(archive_id) / "skill"
                    self.assertFalse(path.exists())
                    self.assertEqual({p.relative_to(payload).as_posix(): p.read_bytes() for p in payload.rglob("*") if p.is_file()}, original)
                    self.assertNotIn("review", [skill.name for skill in discover_skills()[0]])
                    self.assertIn("未知技能", execute_tool("skill", {"skill_name": "review"}))
                    yield "/review 已归档不能调用"
                    yield f"/skill .personal/skill-archives/{archive_id}/skill/SKILL.md"
                    yield "/skill-archives"
                    self.assertEqual(len(requests), checkpoints["before_archive_requests"])
                    yield f"/skill-restore {archive_id}"
                    yield f"/skill-restore {archive_id}"
                    self.assertEqual({p.relative_to(path.parent).as_posix(): p.read_bytes() for p in path.parent.rglob("*") if p.is_file()}, original)
                    self.assertEqual(store.archive_record(archive_id)["status"], "restored")
                    self.assertEqual(SkillLifecycleStore().statistics("review")[0]["counts"]["inline_loaded"], 2)
                    yield "exit"

                output, errors = io.StringIO(), io.StringIO()
                with patch("personal_agent.main.get_model_message", side_effect=model), patch("builtins.input", side_effect=inputs()), redirect_stdout(output), redirect_stderr(errors):
                    history = run_repl(permission_mode="plan", session_name="lifecycle", with_tools=True)
                self.assertTrue(history)
                counts_before_resume = store.statistics()
                with patch("personal_agent.main.get_model_message") as request, patch("builtins.input", side_effect=["/skill-stats", "/clear", "exit"]), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    resumed = run_repl(session_name="lifecycle", resume=True)
                request.assert_not_called()
                self.assertEqual(resumed, [])
                self.assertEqual(store.statistics(), counts_before_resume)
                self.assertIn("项目技能已归档", output.getvalue())
                self.assertIn("当前选中", output.getvalue())
                self.assertIn("技能调用失败", errors.getvalue())

    def test_user_shadow_restore_conflicts_and_failed_persistence_keep_truthful_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch("personal_agent.skills.PROJECT_ROOT", root), patch("personal_agent.skills.USER_SKILLS_ROOT", root / "user-skills"):
                path = create_project_skill("review", "检查", "METHOD")
                original = path.read_bytes()
                store = SkillLifecycleStore()
                with patch.object(Path, "rename", side_effect=OSError("move failed")):
                    with self.assertRaises(OSError):
                        store.archive("review")
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(store.list_archives(), [])
                user = root / "user-skills/review/SKILL.md"
                user.parent.mkdir(parents=True)
                user.write_bytes(original)
                with self.assertRaisesRegex(ValueError, "用户技能"):
                    store.archive("review")
                execute_tool("skill", {"skill_name": "review"})
                user.unlink()
                execute_tool("skill", {"skill_name": "review"})
                self.assertEqual({row["source"] for row in store.statistics("review")}, {"user", "project"})
                record = store.archive("review")
                archive_id = record["id"]
                path.parent.mkdir()
                with self.assertRaisesRegex(ValueError, "不会覆盖"):
                    store.restore(archive_id)
                path.parent.rmdir()
                user.write_bytes(original)
                with self.assertRaisesRegex(ValueError, "同名"):
                    store.restore(archive_id)
                user.unlink()
                payload = store._archive_path(archive_id) / "skill/SKILL.md"
                payload.write_bytes(b"tampered")
                with self.assertRaisesRegex(ValueError, "指纹"):
                    store.restore(archive_id)
                payload.write_bytes(original)
                with patch.object(store, "_write", side_effect=OSError("receipt failed")):
                    with self.assertRaisesRegex(RuntimeError, "已恢复"):
                        store.restore(archive_id)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(store.archive_record(archive_id)["status"], "archived")
                self.assertEqual(store.restore(archive_id)["status"], "restored")

                errors = io.StringIO()
                with patch("personal_agent.skill_lifecycle.SkillLifecycleStore._write", side_effect=OSError("logging failed")), redirect_stderr(errors):
                    self.assertIn("METHOD", execute_tool("skill", {"skill_name": "review"}))
                self.assertIn("计数可能不完整", errors.getvalue())
                prepared = PreparedSkillCall(replace(load_skill(path), context="fork"), "", "METHOD", "manual")
                with self.assertRaisesRegex(RuntimeError, "model failed"):
                    run_fork_skill(prepared, lambda *_: (_ for _ in ()).throw(RuntimeError("model failed")), lambda *_: "")
                counts = store.statistics("review")[0]["counts"]
                self.assertEqual(counts["fork_started"], 1)
                self.assertEqual(counts["fork_returned"], 0)


if __name__ == "__main__":
    unittest.main()
