"""原版/候选回放；--judge 加裁判；--live 才请求真实模型，不采纳候选。"""

import argparse
import hashlib
import io
import json
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from personal_agent.config import load_model_config
from personal_agent.llm import get_model_message
from personal_agent.main import run_repl
from personal_agent.skill_eval import JUDGE_PROMPT, SkillEvaluator
from personal_agent.skill_evolution import SkillEvolutionStore


def run_check(*, live=False, judge=False):
    config = load_model_config() if live else None
    source_path = ROOT / "codex/skill-evolution-live-check-2026-10-28.json"
    source_bytes = source_path.read_bytes()
    source = json.loads(source_bytes)
    requests = []
    output, errors = io.StringIO(), io.StringIO()
    with tempfile.TemporaryDirectory(prefix="personal-agent-skill-eval-") as temporary:
        root = Path(temporary).resolve()
        with patch("personal_agent.skills.PROJECT_ROOT", root), \
             patch("personal_agent.skills.USER_SKILLS_ROOT", root / "isolated-user-skills"), \
             patch("personal_agent.main.get_model_settings", return_value={key: value for key, value in config.items() if key != "api_key"} if config else {"protocol": "openai", "model": "scripted", "base_url": "https://example.invalid"}):
            path = root / ".personal/skills/explain-flow/SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_bytes(source["original_text"].encode("utf-8"))
            before = path.read_bytes()
            store = SkillEvolutionStore()
            feedback = store.record_feedback("explain-flow", source["feedback"]["feedback"], [
                {"role": "user", "content": source["feedback"]["task"]},
                {"role": "assistant", "content": source["feedback"]["answer"]},
            ])
            # 复用上一步已保存的真实草稿，不再生成候选，也不向生成器提供回放样本。
            candidate = store.draft(feedback["id"], lambda _: {"role": "assistant", "content": json.dumps({
                "lesson": source["candidate"]["lesson"], "rationale": source["candidate"]["rationale"],
            }, ensure_ascii=False)}, model_name="replayed-saved-" + source["model"])
            candidate_bytes = store._path("candidate", candidate["id"]).read_bytes()
            suite_path = root / ".personal/skill-evals/suites/explain-holdout.json"
            suite_path.parent.mkdir(parents=True)
            suite_path.write_bytes((ROOT / "examples/evals/explain-holdout.json").read_bytes())

            def model(messages, definitions=None, *, on_usage, **options):
                requests.append({"messages_count": len(messages), "tools_provided": bool(definitions), "options": options})

                def capture(usage):
                    requests[-1]["usage"] = usage
                    on_usage(usage)

                if live:
                    return get_model_message(messages, definitions, on_usage=capture, **options)
                capture({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                if messages[0]["content"] == JUDGE_PROMPT:
                    answer = json.loads(messages[1]["content"])["answer"]
                    return {"role": "assistant", "content": json.dumps({"score": 4 if "调用顺序" in answer else 2, "reason": "固定裁判：检查要点覆盖，不是真实语义评分。"}, ensure_ascii=False)}
                text = ("参数：n 为必填整数，factor 默认为 2。\n返回值：n * factor。\n调用顺序：controller → scale_count → renderer。"
                        if "候选补充经验" in messages[0]["content"] else "scale_count 返回两个整数的乘积。")
                return {"role": "assistant", "content": text}

            evaluator = SkillEvaluator()

            def inputs():
                yield f"{'/skill-eval-judge' if judge else '/skill-eval'} {candidate['id']} {suite_path}"
                report = evaluator.list_reports()[0]
                yield f"/skill-evals {report['id']}"
                if judge:
                    yield "/skill-champions"
                yield "/usage"
                yield "exit"

            with patch("personal_agent.main.get_model_message", side_effect=model), \
                 patch("builtins.input", side_effect=inputs()), redirect_stdout(output), redirect_stderr(errors):
                history = run_repl(permission_mode="plan")
            report = evaluator.list_reports()[0]
            success = (
                report["status"] == "complete" and len(requests) == (4 if judge else 2)
                and all(not request["tools_provided"] for request in requests)
                and all(row["status"] == "complete" and "output" in row for row in report["rows"])
                and report["active_skill_unchanged"] and path.read_bytes() == before
                and store._path("candidate", candidate["id"]).read_bytes() == candidate_bytes
                and store.candidate(candidate["id"])["status"] == "pending" and history == []
                and not errors.getvalue()
            )
            champions = evaluator.list_champions()
            if judge:
                success = success and all(row["judge"]["status"] == "complete" for row in report["rows"]) and bool(champions) == report["selection"]["champion_eligible"]
            return {
                "validation": ("live text replay" + (" and live model judgments" if judge else "") + "; candidate lesson reused from saved previous-stage live draft") if live else "fixed replay/judge responses and usage; no network request",
                "actual_components": "isolated temporary project, run_repl, original/candidate methods, fixed suite, rule scoring, immutable report and local review",
                "model": config["model"] if config else "scripted", "api_key_present": bool(config["api_key"]) if config else None,
                "candidate_origin": {"path": source_path.relative_to(ROOT).as_posix(), "sha256": hashlib.sha256(source_bytes).hexdigest(), "fresh_draft_requests": 0},
                "requests": requests, "evaluation": report,
                "champions": champions,
                "candidate_remained_pending": store.candidate(candidate["id"])["status"] == "pending",
                "active_skill_unchanged": path.read_bytes() == before,
                "conversation_messages_added": len(history), "stderr": errors.getvalue(),
                "evaluation_boundary": "One authored toy sample reused from earlier demo, single pair, text-only; optional same-model judge, no untouched holdout guarantee or tool-task/generalization/cost-improvement claim",
                "success": success,
            }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="使用当前真实模型回放两个版本，不生成或采纳候选")
    parser.add_argument("--judge", action="store_true", help="另为两份回答各请求一次裁判，符合条件才留档 champion")
    args = parser.parse_args()
    report = run_check(live=args.live, judge=args.judge)
    label = "live-" if args.live else ""
    kind = "skill-judge" if args.judge else "skill-eval"
    path = ROOT / f"codex/{kind}-{label}check-2026-10-28.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"report": str(path), "model": report["model"], "model_requests": len(report["requests"]),
                      "decision": report["evaluation"]["decision"], "success": report["success"]}, ensure_ascii=False))
    if not report["success"]:
        raise SystemExit(1)
