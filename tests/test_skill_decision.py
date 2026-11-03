"""两条主流程：三类决策/显式采纳，以及目标、去重与回执恢复边界。"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from personal_agent.main import run_repl
from personal_agent.skill_eval import SkillEvaluator
from personal_agent.skill_evolution import DRAFT_PROMPT, SkillEvolutionStore
from personal_agent.skills import create_project_skill, discover_skills, load_skill


HISTORY = [{"role": "user", "content": "SOURCE_TASK"}, {"role": "assistant", "content": "SOURCE_ANSWER"}]


def proposal(action, name="", lesson="", description="", rationale="基于用户纠正提炼持久方法。"):
    return {"role": "assistant", "content": json.dumps({"action": action, "target_skill": name, "description": description, "lesson": lesson, "rationale": rationale}, ensure_ascii=False)}


class SkillDecisionSmokeTests(unittest.TestCase):
    def test_cli_add_merge_duplicate_and_background_discard_do_not_write_before_adoption(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch("personal_agent.skills.PROJECT_ROOT", root), patch("personal_agent.skills.USER_SKILLS_ROOT", root / "user-skills"):
                source = create_project_skill("explain-flow", "解释", "SOURCE_METHOD $ARGUMENTS")
                target = create_project_skill("debug-flow", "排错", "TARGET_METHOD $ARGUMENTS")
                source_raw, target_raw = source.read_bytes(), target.read_bytes()
                user = root / "user-skills/shared/SKILL.md"
                user.parent.mkdir(parents=True)
                user.write_text("---\ndescription: 共享说明\n---\nUSER_BODY_NOT_SENT", encoding="utf-8")
                store = SkillEvolutionStore()
                lesson = "先区分已验证结果与猜测，再说明如何验证。"

                def model(messages, definitions=None, *, on_usage, **options):
                    on_usage({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                    if messages[0]["content"] != DRAFT_PROMPT:
                        return {"role": "assistant", "content": "SOURCE_ANSWER"}
                    self.assertIsNone(definitions)
                    self.assertNotIn("USER_BODY_NOT_SENT", str(messages))
                    data = json.loads(messages[1]["content"])
                    text = data["feedback_record"]["feedback"]
                    if text.startswith("ADD"):
                        return proposal("add", "format-output", "先给结论，再列依据：$ARGUMENTS", "结构化报告")
                    if text.startswith("THANKS"):
                        return proposal("discard", rationale="弱确认，没有新的持久方法。")
                    return proposal("merge", "debug-flow", lesson)

                def submit(text):
                    yield "/feedback explain-flow " + text
                    feedback = store.list_records("feedback")[-1]
                    yield f"/skill-draft {feedback['id']}"

                def inputs():
                    yield "SOURCE_TASK"
                    yield from submit("ADD 一种独立报告格式")
                    added = store.list_records("candidate")[0]
                    self.assertEqual(added["action"], "add")
                    self.assertFalse((root / added["skill_path"]).exists())
                    with self.assertRaisesRegex(ValueError, "尚无原版"):
                        SkillEvaluator()._versions(added["id"])
                    yield f"/skill-candidates {added['id']}"
                    yield f"/skill-adopt {added['id']}"
                    created = (root / added["skill_path"]).read_bytes()
                    yield f"/skill-adopt {added['id']}"
                    self.assertEqual((root / added["skill_path"]).read_bytes(), created)
                    self.assertEqual(load_skill(root / added["skill_path"]).context, "inline")
                    self.assertIn("format-output", [item.name for item in discover_skills()[0]])
                    yield from submit("MERGE 向排错方法补充验证习惯")
                    merged = next(item for item in store.list_records("candidate") if item.get("action") == "merge")
                    self.assertEqual(merged["source"]["skill_name"], "explain-flow")
                    self.assertEqual(merged["skill_name"], "debug-flow")
                    self.assertEqual(target.read_bytes(), target_raw)
                    versions = SkillEvaluator()._versions(merged["id"])[3]
                    self.assertIn("TARGET_METHOD", versions["baseline"].prompt_template)
                    self.assertNotIn("SOURCE_METHOD", versions["baseline"].prompt_template)
                    yield from submit("DUP_PENDING 同一段经验")
                    self.assertEqual(len(store.list_records("candidate")), 2)
                    self.assertTrue(any(item["duplicate_of"] == "candidate:" + merged["id"] for item in store.list_records("decision")))
                    yield f"/skill-adopt {merged['id']}"
                    receipt = store.candidate(merged["id"])
                    self.assertEqual((root / receipt["snapshot"]).read_bytes(), target_raw)
                    yield from submit("DUP_ACTIVE 已经写入的相同段落")
                    self.assertEqual(len(store.list_records("candidate")), 2)
                    yield "/skill-learn explain-flow"
                    yield "THANKS 谢谢"
                    yield "/skill-jobs wait"
                    job = json.loads(next((root / ".personal/skill-jobs").glob("*.json")).read_text(encoding="utf-8"))
                    self.assertEqual(job["status"], "discarded")
                    self.assertIsNone(job["candidate_id"])
                    yield "/skill-decisions"
                    yield f"/skill-decisions {job['decision_id']}"
                    yield "/usage"
                    yield "exit"

                output, errors = io.StringIO(), io.StringIO()
                with patch("personal_agent.main.get_model_message", side_effect=model), patch("builtins.input", side_effect=inputs()), redirect_stdout(output), redirect_stderr(errors):
                    history = run_repl(permission_mode="plan")
                self.assertEqual(history, HISTORY)
                self.assertEqual(source.read_bytes(), source_raw)
                self.assertEqual(len(store.list_records("decision")), 5)
                self.assertEqual(target.read_bytes().count(lesson.encode()), 1)
                self.assertIn("逻辑调用 6 次", output.getvalue())
                self.assertIn("action=discard", output.getvalue())
                self.assertEqual(errors.getvalue(), "")

    def test_changed_targets_collisions_replays_and_failed_receipts_preserve_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch("personal_agent.skills.PROJECT_ROOT", root), patch("personal_agent.skills.USER_SKILLS_ROOT", root / "user-skills"):
                source = create_project_skill("source", "来源", "SOURCE")
                source_raw = source.read_bytes()
                target = create_project_skill("target", "目标", "TARGET")
                raw = target.read_bytes()
                store = SkillEvolutionStore()
                feedback = store.record_feedback("source", "纠正", HISTORY)
                request = store.prepare_draft(feedback["id"])
                merged = proposal("merge", "target", "追加独立验证步骤。")
                target.write_bytes(raw + b"\nexternal")
                with self.assertRaisesRegex(ValueError, "版本已变化"):
                    store.save_draft(feedback["id"], merged, request=request)
                target.write_bytes(raw)
                with self.assertRaisesRegex(ValueError, "名称已存在"):
                    store.save_draft(feedback["id"], proposal("add", "target", "全新方法", "新说明"), request=request)
                with self.assertRaisesRegex(ValueError, "必须为空"):
                    store.save_draft(feedback["id"], proposal("discard", lesson="不应存在"), request=request)
                self.assertEqual(store.list_records("decision"), [])
                candidate = store.save_draft(feedback["id"], merged, request=request)
                self.assertEqual(store.save_draft(feedback["id"], merged, request=request)["id"], candidate["id"])
                self.assertEqual(len(store.list_records("candidate")), 1)
                self.assertEqual(len(store.list_records("decision")), 1)
                store.reject(candidate["id"])
                self.assertEqual(store.save_draft(feedback["id"], merged, request=request)["status"], "rejected")

                add_reply = proposal("add", "new-method", "先整理证据。\n\n再区分假设。", "整理证据")
                real_save = store._save
                def fail_candidate(record, **kwargs):
                    if record["kind"] == "candidate":
                        raise OSError("candidate save failed")
                    return real_save(record, **kwargs)
                with patch.object(store, "_save", side_effect=fail_candidate):
                    with self.assertRaises(OSError):
                        store.save_draft(feedback["id"], add_reply, request=request)
                decision = next(item for item in store.list_records("decision") if item["action"] == "add")
                added = store.save_draft(feedback["id"], add_reply, request=request)
                self.assertEqual(added["id"], decision["candidate_id"])
                self.assertFalse((root / added["skill_path"]).exists())
                collision = create_project_skill("new-method", "其他说明", "OTHER_CONTENT")
                before = collision.read_bytes()
                with self.assertRaisesRegex(ValueError, "不匹配"):
                    store.adopt(added["id"])
                self.assertEqual(collision.read_bytes(), before)
                collision.unlink()
                collision.parent.rmdir()
                with patch.object(store, "_save", side_effect=OSError("receipt failed")):
                    with self.assertRaisesRegex(RuntimeError, "新技能已创建.*回执"):
                        store.adopt(added["id"])
                created = (root / added["skill_path"]).read_bytes()
                self.assertEqual(store.candidate(added["id"])["status"], "pending")
                self.assertEqual(store.adopt(added["id"])["status"], "adopted")
                self.assertEqual(store.adopt(added["id"])["created_sha256"], store.candidate(added["id"])["created_sha256"])
                self.assertEqual((root / added["skill_path"]).read_bytes(), created)
                self.assertIn("先整理证据。\n\n再区分假设。", load_skill(root / added["skill_path"]).prompt_template)
                self.assertEqual(target.read_bytes(), raw)
                self.assertEqual(source.read_bytes(), source_raw)


if __name__ == "__main__":
    unittest.main()
