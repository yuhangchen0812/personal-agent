"""三条主流程：反馈到采纳、版本和写入失败保护，以及草稿预算边界。"""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from personal_agent.main import run_repl
from personal_agent.session import Session
from personal_agent.skill_evolution import DRAFT_PROMPT, SkillEvolutionStore
from personal_agent.skills import create_project_skill, load_skill
from personal_agent.usage import TokenPrices


def draft_reply(lesson="说明参数含义、返回值及调用顺序，再给出一个有依据的例子。"):
    return {"role": "assistant", "content": json.dumps({"lesson": lesson, "rationale": "用户反馈指出解释遗漏，应将解释步骤加入后续方法。"}, ensure_ascii=False)}


class SkillEvolutionSmokeTests(unittest.TestCase):
    def test_repl_feedback_draft_review_adopt_and_future_skill_call(self):
        requests, draft_requests, snapshots = [], [], []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            user_root = root / "isolated-user/skills"
            path = root / ".personal/skills/explain-flow/SKILL.md"
            path.parent.mkdir(parents=True)
            original = ("\ufeff---\r\nname: explain-flow\r\ndescription: 解释调用流程\r\n"
                        "allowed-tools: read_file\r\ncontext: inline\r\n---\r\n解释任务：$ARGUMENTS\r\n").encode("utf-8")
            path.write_bytes(original)
            lesson = "说明参数含义、返回值及调用顺序，再给出一个有依据的例子。"

            def model(messages, definitions=None, *, on_usage, **_):
                on_usage({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                if messages[0]["content"] == DRAFT_PROMPT:
                    self.assertIsNone(definitions)
                    self.assertNotIn("PRIVATE_PRIOR_TURN", str(messages))
                    self.assertIn("缺少返回值说明", str(messages))
                    self.assertIn("解释任务：$ARGUMENTS", str(messages))
                    draft_requests.append(copy.deepcopy(messages))
                    return draft_reply(lesson)
                requests.append(copy.deepcopy(messages))
                return {"role": "assistant", "content": "固定任务解释，尚未包含反馈补充。"}

            output, errors = io.StringIO(), io.StringIO()
            with patch("personal_agent.skills.PROJECT_ROOT", root), \
                 patch("personal_agent.skills.USER_SKILLS_ROOT", user_root), \
                 patch("personal_agent.session.PROJECT_ROOT", root), \
                 patch("personal_agent.main.discover_agents", return_value=([], [])), \
                 patch("personal_agent.main.get_model_message", side_effect=model), \
                 redirect_stdout(output), redirect_stderr(errors):
                store = SkillEvolutionStore()

                def inputs():
                    yield "PRIVATE_PRIOR_TURN"
                    yield "/explain-flow demo.py"
                    yield "/feedback explain-flow 缺少返回值说明，请补充参数、返回值与调用顺序。"
                    feedback = store.list_records("feedback")[0]
                    self.assertIn("demo.py", feedback["task"])
                    self.assertNotIn("PRIVATE_PRIOR_TURN", str(feedback))
                    yield "/feedback"
                    yield f"/skill-draft {feedback['id']}"
                    candidate = store.list_records("candidate")[0]
                    self.assertEqual(candidate["status"], "pending")
                    self.assertEqual(path.read_bytes(), original)
                    self.assertEqual(len(Session("evolution").load()), 4)
                    yield "/skill-candidates"
                    yield f"/skill-candidates {candidate['id']}"
                    yield f"/skill-adopt {candidate['id']}"
                    receipt = store.candidate(candidate["id"])
                    snapshot = root / receipt["snapshot"]
                    snapshots.append(snapshot)
                    self.assertEqual(snapshot.read_bytes(), original)
                    updated = path.read_bytes()
                    self.assertTrue(updated.startswith(original))
                    self.assertEqual(updated.count(candidate["id"].encode()), 1)
                    self.assertEqual(load_skill(path).allowed_tools, ("read_file",))
                    yield f"/skill-adopt {candidate['id']}"
                    self.assertEqual(path.read_bytes(), updated)
                    yield "/explain-flow next.py"
                    yield "/usage"
                    yield "exit"

                with patch("builtins.input", side_effect=inputs()):
                    history = run_repl(session_name="evolution", with_tools=True, permission_mode="plan")
                self.assertEqual(Session("evolution").load(), history)
                self.assertEqual(len(history), 6)
                self.assertNotIn(lesson, history[2]["content"])
                self.assertIn(lesson, history[4]["content"])
                self.assertNotIn("/skill-adopt", str(history))
                self.assertEqual(len(list(snapshots[0].parent.glob("*.md"))), 1)
                with self.assertRaises(ValueError):
                    create_project_skill("feedback", "不能占用命令", "正文")
            self.assertEqual((len(requests), len(draft_requests)), (3, 1))
            self.assertIn("逻辑调用 4 次", output.getvalue())
            self.assertIn("尚未采纳", output.getvalue())
            self.assertEqual(errors.getvalue(), "")

    def test_rejected_stale_and_failed_adoptions_do_not_overwrite_or_repeat(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch("personal_agent.skills.PROJECT_ROOT", root), patch("personal_agent.skills.USER_SKILLS_ROOT", root / "user-skills"):
                path = create_project_skill("explain", "解释代码", "解释 $ARGUMENTS")
                original = path.read_bytes()
                store = SkillEvolutionStore()
                history = [{"role": "user", "content": "说明参数"}, {"role": "assistant", "content": "原回答"}]
                with self.assertRaises(ValueError):
                    store.record_feedback("explain", "反馈", [])
                feedback = store.record_feedback("explain", "缺少调用顺序", history)
                with self.assertRaisesRegex(ValueError, "JSON"):
                    store.draft(feedback["id"], lambda _: {"role": "assistant", "content": "not json"})
                with self.assertRaisesRegex(ValueError, "工具请求不会执行"):
                    store.draft(feedback["id"], lambda _: {"role": "assistant", "tool_calls": [{"id": "do_not_run"}]})
                self.assertEqual(store.list_records("candidate"), [])
                candidate = store.draft(feedback["id"], lambda _: draft_reply())
                path.write_bytes(original + b"\nexternal edit\n")
                changed = path.read_bytes()
                with self.assertRaisesRegex(ValueError, "版本已变化"):
                    store.adopt(candidate["id"])
                self.assertEqual(path.read_bytes(), changed)
                self.assertFalse((root / ".personal/skill-history").exists())
                path.write_bytes(original)
                store.reject(candidate["id"])
                with self.assertRaisesRegex(ValueError, "已拒绝"):
                    store.adopt(candidate["id"])
                retry = store.draft(feedback["id"], lambda _: draft_reply())
                with patch("personal_agent.skills.os.replace", side_effect=OSError("模拟替换失败")):
                    with self.assertRaises(OSError):
                        store.adopt(retry["id"])
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(store.candidate(retry["id"])["status"], "pending")
                receipt = store.adopt(retry["id"])
                self.assertEqual((root / receipt["snapshot"]).read_bytes(), original)
                before_receipt_failure = path.read_bytes()
                fresh_feedback = store.record_feedback("explain", "补充验证依据", history)
                final = store.draft(fresh_feedback["id"], lambda _: draft_reply("引用实际证据，区分已验证结果和待核实结论。"))
                with patch.object(store, "_save", side_effect=OSError("模拟回执失败")):
                    with self.assertRaisesRegex(RuntimeError, "技能已更新.*回执保存失败"):
                        store.adopt(final["id"])
                applied = path.read_bytes()
                self.assertEqual(applied.count(final["id"].encode()), 1)
                self.assertEqual(store.candidate(final["id"])["status"], "pending")
                recovered = store.adopt(final["id"])
                self.assertEqual(recovered["status"], "adopted")
                self.assertEqual(path.read_bytes(), applied)
                self.assertEqual((root / recovered["snapshot"]).read_bytes(), before_receipt_failure)
                self.assertEqual(len(list((root / ".personal/skill-history/explain").glob("*.md"))), 2)
                self.assertEqual(list(path.parent.glob(".evolve-*.tmp")), [])

    def test_zero_budget_blocks_draft_model_but_local_records_remain_available(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch("personal_agent.skills.PROJECT_ROOT", root), patch("personal_agent.skills.USER_SKILLS_ROOT", root / "user-skills"):
                path = create_project_skill("explain", "解释", "解释 $ARGUMENTS")
                original = path.read_bytes()
                store = SkillEvolutionStore()
                feedback = store.record_feedback("explain", "反馈", [{"role": "user", "content": "任务"}, {"role": "assistant", "content": "结果"}])
                output, errors = io.StringIO(), io.StringIO()
                with patch("personal_agent.main.get_model_message") as model, \
                     patch("builtins.input", side_effect=["/feedback", f"/skill-draft {feedback['id']}", "/skill-candidates", "exit"]), \
                     redirect_stdout(output), redirect_stderr(errors):
                    history = run_repl(prices=TokenPrices(Decimal(1), Decimal(1)), max_cost=Decimal(0))
                model.assert_not_called()
                self.assertEqual(history, [])
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(store.list_records("candidate"), [])
                self.assertIn(feedback["id"], output.getvalue())
                self.assertIn("预算", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
