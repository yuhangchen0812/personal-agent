"""固定来源的三类 Skill 决策演示；--live 发送三次真实分类/提炼请求，均不采纳。"""

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
from personal_agent.skill_evolution import DRAFT_PROMPT, SkillEvolutionStore
from personal_agent.skills import create_project_skill


def run_check(*, live=False):
    config = load_model_config() if live else None
    output, errors, requests = io.StringIO(), io.StringIO(), []
    cases = [
        "以后解释函数时，必须分别说明参数、默认值、返回值和调用顺序，只依据给定材料，缺失实现标记待确认。",
        "我还需要一种与函数解释不同的独立工作流程：处理任何交接记录时，按负责人、当前状态、阻塞原因、下一步四项整理，缺失字段标记待补充。请沉淀为适用于后续所有交接记录的独立清单。",
        "谢谢，这次先这样。",
    ]
    if not live:
        cases.append("再次提出完全相同的函数解释步骤。")
    with tempfile.TemporaryDirectory(prefix="personal-agent-decision-") as temporary:
        root = Path(temporary).resolve()
        with patch("personal_agent.skills.PROJECT_ROOT", root), patch("personal_agent.skills.USER_SKILLS_ROOT", root / "isolated-user-skills"), patch("personal_agent.main.get_model_settings", return_value={key: value for key, value in config.items() if key != "api_key"} if config else {"protocol": "openai", "model": "scripted", "base_url": "https://example.invalid"}):
            path = create_project_skill("explain-flow", "解释给定函数的参数、返回值和调用关系", "解释给定函数的功能：$ARGUMENTS")
            original = path.read_bytes()
            store = SkillEvolutionStore()

            def model(messages, definitions=None, *, on_usage, **options):
                if messages[0]["content"] != DRAFT_PROMPT:
                    on_usage({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                    return {"role": "assistant", "content": "固定源回答：这个函数完成计算。"}
                entry = {"messages_count": len(messages), "tools_provided": bool(definitions), "options": options}
                requests.append(entry)
                def capture(usage):
                    entry["usage"] = usage
                    on_usage(usage)
                if live:
                    response = get_model_message(messages, on_usage=capture, **options)
                else:
                    feedback = json.loads(messages[1]["content"])["feedback_record"]["feedback"]
                    value = {"action": "discard", "target_skill": "", "description": "", "lesson": "", "rationale": "弱确认，没有新的持久方法。"}
                    if feedback.startswith(("以后解释", "再次提出")):
                        value.update(action="merge", target_skill="explain-flow", lesson="依次解释参数、默认值、返回值与调用顺序，未知实现标记待确认。", rationale="纠正解释步骤的遗漏。")
                    elif feedback.startswith("我还需要"):
                        value.update(action="add", target_skill="handover-checklist", description="整理交接记录", lesson="按负责人、当前状态、阻塞原因、下一步整理交接内容，缺失字段标记待补充。", rationale="不同于函数解释的独立工作流程。")
                    capture({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                    response = {"role": "assistant", "content": json.dumps(value, ensure_ascii=False)}
                entry["raw_response"] = response.get("content")
                return response

            def inputs():
                yield "解释给定函数 scale_count(n, factor=2)，返回 n * factor，由 controller 调用后交给 renderer。"
                for feedback in cases:
                    yield "/feedback explain-flow " + feedback
                    record = store.list_records("feedback")[-1]
                    yield f"/skill-draft {record['id']}"
                yield "/skill-decisions"
                yield "/skill-candidates"
                yield "/usage"
                yield "exit"

            with patch("personal_agent.main.get_model_message", side_effect=model), patch("builtins.input", side_effect=inputs()), redirect_stdout(output), redirect_stderr(errors):
                history = run_repl(permission_mode="plan")
            decisions, candidates = store.list_records("decision"), store.list_records("candidate")
            unchanged = path.read_bytes() == original and all(not (root / candidate["skill_path"]).exists() for candidate in candidates if candidate["action"] == "add")
            known = [entry["usage"] for entry in requests if entry.get("usage")]
            return {"validation": "three live decision requests; fixed source task/answer; real CLI and persistence" if live else "four fixed decision responses; real CLI, dedup and persistence; no network requests",
                    "model": config["model"] if live else "scripted", "feedback_cases": cases,
                    "requests": requests, "decisions": decisions, "candidates": candidates,
                    "reported_decision_tokens": {key: sum(item[key] for item in known) for key in ("prompt_tokens", "completion_tokens", "total_tokens")} if known else None,
                    "active_skills_unchanged": unchanged, "original_sha256": hashlib.sha256(original).hexdigest(),
                    "history_messages": len(history), "stderr": errors.getvalue(), "stdout": output.getvalue(),
                    "boundary": "Authored examples, fixed source answer; actions and reasons recorded, no skill-quality/generalization claim; no adoption; temporary project removed.",
                    "success": unchanged and not errors.getvalue() and len(decisions) == len(cases) and len(requests) == len(cases)
                               and all(item["status"] == "pending" for item in candidates) and len(history) == 2}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    report = run_check(live=args.live)
    path = ROOT / f"codex/skill-decision-{'live-' if args.live else ''}check-2026-10-28.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"report": str(path), "actions": [item["action"] for item in report["decisions"]], "success": report["success"]}, ensure_ascii=False))
    if not report["success"]:
        raise SystemExit(1)
