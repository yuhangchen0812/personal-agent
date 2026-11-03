"""应用 Skill 闭环的固定验证；--live 在独立持久演示项目实际运行，不修改活动 Skill。"""

import argparse
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex.verify_ci_tasks import PACKAGE, OUTPUT, copy_package, fixed_model
from codex.verify_delivery import isolated
from personal_agent import task_runner
from personal_agent import task_learning
from personal_agent.skill_evolution import SkillEvolutionStore
from personal_agent.task_learning import (adopt_task_candidate, compare_task_candidate, digest, draft_task_candidate,
    install_task_skill, record_task_feedback)
from personal_agent.usage import TokenPrices


FEEDBACK = ("本次报告内容正确；这是新增质量要求，不是已观察到的错误：写入前先形成最终测试到状态、物理行号、消息的映射，"
    "再从同一映射派生计数和失败列表，交叉核对失败数与失败列表长度，并逐项核对引用行与最终有效失败记录一致。"
    "将交叉校验沉淀为通用流程，避免复制本次测试名、运行 ID 或输入载荷，不声称效果已经提升。")
LESSON = "生成结构化汇总前，从同一个最终有效记录映射派生计数与明细，交叉核对失败数和失败明细长度，并逐项核对证据位置与选中记录一致；不一致时先修正再写入和读回。"


def draft_model(messages, definitions=None, *, on_usage, **_):
    assert not definitions and "expected_files" not in str(messages)
    payload = json.loads(messages[1]["content"])
    assert payload["feedback_record"]["task_source"]["origin"] == "authored demonstration feedback"
    on_usage({"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3})
    return {"role": "assistant", "content": json.dumps({"action": "merge", "target_skill": "ci-triage", "description": "",
        "lesson": LESSON, "rationale": "明确反馈要求增加跨字段和引用的一致性检查，尚未验证收益。"}, ensure_ascii=False)}


def source(root, *, live=False):
    install = install_task_skill(PACKAGE)
    active = Path(install["path"])
    before = active.read_bytes()
    original = task_runner.run_task_pack(PACKAGE, case_id="dev-dirty", skill_name="ci-triage",
        call_model=None if live else fixed_model())
    assert original["status"] == "passed"
    feedback = record_task_feedback(Path(original["directory"]) / "report.json", "dev-dirty", FEEDBACK,
        origin="authored demonstration feedback")
    return active, before, original, feedback


def flow(root, *, live=False):
    copy_package(root)
    with isolated(root), patch("personal_agent.task_runner.ROOT", root):
        active, before, original, feedback = source(root, live=live)
        draft = draft_task_candidate(feedback["id"], call_model=None if live else draft_model)
        assert draft["status"] == "complete" and draft["candidate_id"], "model may discard; no forced merge or retry"
        candidate_id = draft["candidate_id"]
        dev = compare_task_candidate(PACKAGE, candidate_id, call_model=None if live else fixed_model())
        assert dev["selected_cases"] == ["dev-retry"] and dev["excluded_source_case"] == "dev-dirty"
        assert active.read_bytes() == before and SkillEvolutionStore().candidate(candidate_id)["status"] == "pending"
        dev_path = Path(dev["directory"]) / "report.json"
        holdout = compare_task_candidate(PACKAGE, candidate_id, split="holdout", development_report=dev_path,
            call_model=None if live else fixed_model())
        assert active.read_bytes() == before
        holdout_path = Path(holdout["directory"]) / "report.json"
        reason = "自编样本上未观察到回退；显式采纳交叉校验步骤，不宣称能力提升。"
        if not live:
            save = task_learning._save_json
            def fail_final_receipt(path, value):
                if value.get("kind") == "adoption" and value["status"] == "adopted":
                    raise OSError("injected receipt failure after actual adoption")
                return save(path, value)
            with patch("personal_agent.task_learning._save_json", side_effect=fail_final_receipt):
                try:
                    adopt_task_candidate(PACKAGE, candidate_id, dev_path, holdout_path, reason)
                except OSError:
                    assert SkillEvolutionStore().candidate(candidate_id)["status"] == "adopted"
                else:
                    raise AssertionError("receipt failure was not injected")
        receipt = adopt_task_candidate(PACKAGE, candidate_id, dev_path, holdout_path, reason)
        after = active.read_bytes()
        assert receipt["status"] == "adopted" and (root / receipt["snapshot"]).read_bytes() == before
        assert adopt_task_candidate(PACKAGE, candidate_id, dev_path, holdout_path, "重复确认") == receipt
        assert active.read_bytes() == after and after.count(candidate_id.encode()) == 1
        following = task_runner.run_task_pack(PACKAGE, split="holdout", case_id="holdout-unicode", skill_name="ci-triage",
            call_model=None if live else fixed_model())
        assert following["status"] == "passed" and following["method"]["sha256"] == digest(after)
        usage = [original["usage"], draft["usage"], dev["usage"], holdout["usage"], following["usage"]]
        return {"status": "passed", "validation": "real model, actual isolated files" if live else "fixed model/usage, actual isolated files",
            "project": str(root), "source": original, "feedback": feedback, "draft": draft, "development": dev,
            "holdout": holdout, "adoption": receipt, "following": following,
            "total_usage": {key: sum(item[key] for item in usage) for key in
                ("calls", "reported_calls", "prompt_tokens", "completion_tokens", "total_tokens", "missing_calls")},
            "estimated_cost_yuan": None, "source_case_excluded": True, "no_automatic_adoption": True,
            "boundary": "authored feedback added after a correct source result; observed authored samples; no blind holdout or generalization/improvement claim"}


def verify_workflow():
    with tempfile.TemporaryDirectory(prefix="task-learning-") as temporary:
        result = flow(Path(temporary).resolve())
        return {"status": result["status"], "source_case_excluded": True,
            "development": result["development"]["decision"], "holdout": result["holdout"]["decision"],
            "snapshot_duplicate_adoption_and_receipt_recovery_verified": True, "following_active_method_verified": True,
            "calls": result["total_usage"]["calls"]}


def rejected(action):
    try:
        action()
    except ValueError:
        return
    raise AssertionError("expected rejection before mutation/model request")


def verify_guards():
    with tempfile.TemporaryDirectory(prefix="task-learning-guards-") as temporary:
        root = Path(temporary).resolve()
        copy_package(root)
        with isolated(root), patch("personal_agent.task_runner.ROOT", root):
            active, before, original, feedback = source(root)
            run_path = Path(original["directory"]) / "report.json"
            heldout_source = task_runner.run_task_pack(PACKAGE, split="holdout", case_id="holdout-unicode", call_model=fixed_model())
            rejected(lambda: record_task_feedback(Path(heldout_source["directory"]) / "report.json", "holdout-unicode", FEEDBACK))
            artifact = Path(original["directory"]) / original["rows"][0]["workspace"] / OUTPUT
            original_artifact = artifact.read_bytes()
            artifact.write_text("{}", encoding="utf-8")
            rejected(lambda: record_task_feedback(run_path, "dev-dirty", FEEDBACK))
            artifact.write_bytes(original_artifact)
            draft = draft_task_candidate(feedback["id"], call_model=draft_model)
            candidate_id = draft["candidate_id"]
            rejected(lambda: compare_task_candidate(PACKAGE, candidate_id, split="holdout", call_model=fixed_model()))
            model = fixed_model()
            zero = compare_task_candidate(PACKAGE, candidate_id, call_model=model,
                prices=TokenPrices(Decimal(1), Decimal(1)), max_cost=Decimal(0))
            assert zero["status"] == "partial" and not model.calls and zero["rows"][1]["status"] == "not_run"
            cancelled = compare_task_candidate(PACKAGE, candidate_id, call_model=fixed_model("cancel"))
            assert cancelled["status"] == "cancelled" and cancelled["rows"][1]["status"] == "not_run"
            assert (Path(cancelled["directory"]) / cancelled["rows"][0]["workspace"] / OUTPUT).is_file()
            bad = compare_task_candidate(PACKAGE, candidate_id, call_model=fixed_model("wrong"))
            rejected(lambda: compare_task_candidate(PACKAGE, candidate_id, split="holdout",
                development_report=Path(bad["directory"]) / "report.json", call_model=fixed_model()))
            dev = compare_task_candidate(PACKAGE, candidate_id, call_model=fixed_model())
            dev_path = Path(dev["directory"]) / "report.json"
            holdout = compare_task_candidate(PACKAGE, candidate_id, split="holdout", development_report=dev_path, call_model=fixed_model())
            holdout_path = Path(holdout["directory"]) / "report.json"
            rejected(lambda: adopt_task_candidate(PACKAGE, candidate_id, dev_path, holdout_path, ""))
            candidate_path = SkillEvolutionStore()._path("candidate", candidate_id)
            frozen_candidate = candidate_path.read_bytes()
            changed = json.loads(frozen_candidate)
            changed["lesson"] += " changed"
            candidate_path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
            rejected(lambda: adopt_task_candidate(PACKAGE, candidate_id, dev_path, holdout_path, "test"))
            candidate_path.write_bytes(frozen_candidate)
            saved = dev_path.read_bytes()
            tampered = deepcopy(dev)
            tampered["summary"]["candidate"]["passed"] = 99
            dev_path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
            rejected(lambda: adopt_task_candidate(PACKAGE, candidate_id, dev_path, holdout_path, "test"))
            dev_path.write_bytes(saved)
            out = Path(holdout["directory"]) / holdout["rows"][1]["workspace"] / OUTPUT
            out.write_text("{}", encoding="utf-8")
            rejected(lambda: adopt_task_candidate(PACKAGE, candidate_id, dev_path, holdout_path, "test"))
            assert active.read_bytes() == before and SkillEvolutionStore().candidate(candidate_id)["status"] == "pending"
            return {"status": "passed", "holdout_feedback_rejected": True, "changed_source_artifacts_rejected": True,
                "holdout_requires_development": True, "budget_zero_calls": True, "cancel_keeps_artifact": True,
                "failed_candidate_cannot_advance": True, "reason_required": True,
                "candidate_report_artifact_tampering_blocks_adoption": True, "active_skill_unchanged": True}


def entrypoint():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or ROOT / "codex/acceptance-runs" / f"task-learning-{uuid4().hex}.json"
    if output.exists():
        parser.error("报告已存在，未执行或覆盖。")
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"workflow": verify_workflow(), "guards": verify_guards()}
    if args.live:
        project = ROOT / "codex/task-learning-runs" / uuid4().hex
        project.mkdir(parents=True, exist_ok=False)
        try:
            report["live"] = flow(project, live=True)
        except Exception as error:
            report["live"] = {"status": "failed", "project": str(project), "error": type(error).__name__, "message": str(error)}
    report["status"] = "passed" if all(value["status"] == "passed" for value in report.values()) else "failed"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "output": str(output)}, ensure_ascii=False))
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    entrypoint()
