"""一次集成验证：父链任务、三阶段检查点恢复、幂等、阻止回归与来源变化。"""

from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.support.verify_evidence_limits import SUITE as OLD_SUITE, fixed_model as old_model
from tests.support.verify_rule_evidence import SUITE, NOTE, RULE_NOTE, fixed_model, fixed_reviews, reject
from personal_agent import repair_workflow as flow
from personal_agent.file_runtime import write_json_atomic
from personal_agent.repair_eval import run_evaluation
from personal_agent.source_snapshot import snapshot_sources
from personal_agent.usage import TokenPrices


def verify_workflow():
    with tempfile.TemporaryDirectory(prefix="workflow-中文 空格-") as temporary:
        root = Path(temporary).resolve()
        run_evaluation(OLD_SUITE, NOTE, output_dir=root / "parents", call_model=old_model(), max_turns=4)
        write_json_atomic(root / "parents.json", {"version": 1, "parents": [{"case_id": name, "diagnosis": f"parents/cases/{name}/diagnosis/report.json"} for name in ("dev-metering", "holdout-tax")]})
        task = root / "任务"
        state = flow.create_task(SUITE, NOTE, rules_review_note=RULE_NOTE, parents_path=root / "parents.json", max_turns=4, output_dir=task)
        assert state["status"] == "imported" and not (task / "attempts").exists()
        parent_bytes = (root / "parents/report.json").read_bytes()
        model = fixed_model()
        def crash_completion(*_):
            raise KeyboardInterrupt()
        def interrupted(action, **options):
            try:
                with patch.object(flow, "_complete", crash_completion):
                    flow.task_action(task, action, **options)
            except KeyboardInterrupt:
                return
            raise AssertionError("checkpoint fault not injected")
        def fresh_resume(expected):
            process = subprocess.run([sys.executable, "-X", "utf8", "-B", str(ROOT / "run_task.py"), "workflow", "resume", "--task", "任务"],
                cwd=root, capture_output=True, timeout=60)
            assert process.returncode == 0, process.stderr.decode("utf-8", errors="replace")
            value = json.loads(process.stdout)
            assert value["status"] == expected and value["active"] is None
            return value
        interrupted("diagnose", call_model=model)
        state = fresh_resume("awaiting_review")
        calls = len(model.calls)
        flow.task_action(task, "diagnose", call_model=model)
        assert len(model.calls) == calls == 8
        run = json.loads(Path(state["diagnosis"]["path"]).read_bytes())
        fixed_reviews(run, root / "reviews.json")
        interrupted("verify", reviews=root / "reviews.json")
        state = fresh_resume("evaluated")
        interrupted("publish", review_note=RULE_NOTE)
        state = fresh_resume("published")
        publication = Path(state["publication"]["path"])
        published = json.loads(publication.read_bytes())
        assert published["all_cases_full_fix_verified"] and len(published["cases"]) == 2
        assert all(case["receipt"] and case["history"] for case in published["cases"])
        for case in published["cases"]:
            history = json.loads(Path(case["history"]["path"]).read_bytes())
            assert history["summary"]["rounds"] == 2 and history["summary"]["generation_bound_edges"] == 1
        with patch.object(flow, "verify_evaluation", side_effect=AssertionError("tests replayed")), patch.object(flow, "issue_evaluation_receipt", side_effect=AssertionError("publication repeated")):
            assert flow.task_action(task, "verify", reviews=root / "reviews.json")["status"] == "published"
            assert flow.task_action(task, "publish", review_note=RULE_NOTE)["status"] == "published"
        for target in (task / "inputs/cases/dev-metering/project/metering.py", Path(state["attempts"][0]["directory"]) / "completion.json", publication.parent / "index.html", root / "reviews.json"):
            raw = target.read_bytes(); target.write_bytes(raw + b"\n")
            reject(lambda: flow.task_action(task, "show"))
            target.write_bytes(raw)
        flow.task_action(task, "show")

        # 没有完成标记时只标记中断；明确 retry + 零预算才创建新尝试，不重放旧模型调用。
        second = root / "中断任务"
        flow.create_task(SUITE, NOTE, rules_review_note=RULE_NOTE, max_turns=4, output_dir=second)
        try:
            with patch.object(flow, "run_evaluation", side_effect=KeyboardInterrupt):
                flow.task_action(second, "diagnose", call_model=model)
        except KeyboardInterrupt:
            pass
        assert flow.task_action(second, "resume")["status"] == "interrupted"
        with patch.object(flow, "run_evaluation", side_effect=AssertionError("automatic replay")):
            assert flow.task_action(second, "diagnose", call_model=model)["status"] == "interrupted"
        state2 = flow.task_action(second, "diagnose", retry=True, call_model=model, prices=TokenPrices(Decimal(1), Decimal(1)), max_cost=Decimal(0))
        assert state2["status"] == "diagnosis_incomplete" and len(model.calls) == calls
        assert len(state2["attempts"]) == 2

        # 单例公开通过但违反规则范围，必须保留 regression_detected，发布不能生成采纳凭据。
        blocked = root / "回归任务"
        flow.create_task(SUITE, NOTE, rules_review_note=RULE_NOTE, max_turns=4, split="development", output_dir=blocked)
        good = fixed_model()
        def too_broad(history, definitions, **options):
            response = good(history, definitions, **options)
            if options.get("response_schema") is not None:
                value = json.loads(response["content"])
                for edit in value["edits"]:
                    if edit["rule_ids"]:
                        edit["new"] = "    return 90"
                response["content"] = json.dumps(value, ensure_ascii=False)
            return response
        bad_state = flow.task_action(blocked, "diagnose", call_model=too_broad)
        fixed_reviews(json.loads(Path(bad_state["diagnosis"]["path"]).read_bytes()), root / "bad-reviews.json")
        bad_state = flow.task_action(blocked, "verify", reviews=root / "bad-reviews.json")
        bad_state = flow.task_action(blocked, "publish", review_note=RULE_NOTE)
        value = json.loads(Path(bad_state["publication"]["path"]).read_bytes())
        assert not value["all_cases_full_fix_verified"] and value["cases"][0]["outcome"] == "regression_detected"
        assert value["cases"][0]["receipt"] is None
        assert "regression_detected" in (Path(bad_state["publication"]["path"]).parent / "index.html").read_text(encoding="utf-8")
        assert (root / "parents/report.json").read_bytes() == parent_bytes
        assert snapshot_sources(task / "inputs/cases/dev-metering/project")["manifest"] == snapshot_sources(SUITE.parent / "dev-metering/project")["manifest"]
        return {"status": "passed", "create_parent_bound_diagnose_review_verify_and_publish": True,
            "three_phase_checkpoints_recovered_in_fresh_process_without_replay": True, "missing_completion_requires_explicit_retry_zero_budget_no_calls": True,
            "repeated_commands_do_not_repeat_models_tests_or_publication": True, "changed_input_checkpoint_review_or_html_rejected": True,
            "regression_published_as_failure_without_adoption_receipt": True, "original_source_and_parent_records_unchanged": True, "new_network_calls": 0}
