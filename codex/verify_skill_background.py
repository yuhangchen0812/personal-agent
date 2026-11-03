"""隔离项目的反馈窗口/后台草稿演示；--live 仅将一次后台草稿换成真实请求。"""

import argparse
import hashlib
import io
import json
import sys
import tempfile
import threading
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
    requests = []
    main_thread = threading.get_ident()
    output, errors = io.StringIO(), io.StringIO()
    with tempfile.TemporaryDirectory(prefix="personal-agent-background-") as temporary:
        root = Path(temporary).resolve()
        with patch("personal_agent.skills.PROJECT_ROOT", root), patch("personal_agent.skills.USER_SKILLS_ROOT", root / "isolated-user-skills"), patch("personal_agent.main.get_model_settings", return_value={key: value for key, value in config.items() if key != "api_key"} if config else {"protocol": "openai", "model": "scripted", "base_url": "https://example.invalid"}):
            path = create_project_skill("explain-flow", "解释函数调用关系", "解释 $ARGUMENTS 中给出的函数，区分事实与推测。")
            original = path.read_bytes()
            store = SkillEvolutionStore()

            def model(messages, definitions=None, *, on_usage, **options):
                background = messages[0]["content"] == DRAFT_PROMPT
                request = {"kind": "live_background_draft" if background and live else "fixed_background_draft" if background else "fixed_source_task",
                           "background_thread": threading.get_ident() != main_thread,
                           "messages_count": len(messages), "tools_provided": bool(definitions), "options": options}
                requests.append(request)
                def capture(usage):
                    request["usage"] = usage
                    on_usage(usage)
                if background and live:
                    return get_model_message(messages, on_usage=capture, **options)
                capture({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                return {"role": "assistant", "content": json.dumps({"lesson": "解释函数时依次说明参数、返回值及调用顺序，只依据给定材料。", "rationale": "用户纠正了回答结构的遗漏。"}, ensure_ascii=False) if background else "这个函数返回计算结果。"}

            def inputs():
                yield "解释函数 scale_count(n, factor=2)，其返回 n * factor，由 controller 调用后交给 renderer。"
                yield "/skill-learn explain-flow"
                yield "以后解释这类函数时，必须分别说明参数、返回值和调用顺序，不要只给概括；未提供的实现请标记待确认。"
                yield "/skill-jobs wait"
                candidate = store.list_records("candidate")[0]
                yield f"/skill-candidates {candidate['id']}"
                yield "/usage"
                yield "exit"

            with patch("personal_agent.main.get_model_message", side_effect=model), patch("builtins.input", side_effect=inputs()), redirect_stdout(output), redirect_stderr(errors):
                history = run_repl(permission_mode="plan")
            candidate = store.list_records("candidate")[0]
            feedback = store.feedback(candidate["feedback_id"])
            job = json.loads(next((root / ".personal/skill-jobs").glob("*.json")).read_text(encoding="utf-8"))
            return {"validation": "one live background draft, fixed source task/answer; real CLI, thread, feedback snapshot, candidate and job persistence" if live else "fixed model responses; real CLI, thread and filesystem; no network request",
                    "model": config["model"] if live else "scripted", "requests": requests,
                    "feedback": feedback, "candidate": candidate, "job": job,
                    "history": history, "stdout": output.getvalue(), "stderr": errors.getvalue(),
                    "active_skill_unchanged": original == path.read_bytes(), "original_sha256": hashlib.sha256(original).hexdigest(),
                    "boundary": "Candidate generation only; no automatic adoption, quality improvement, evaluation or model training; temporary project removed after run.",
                    "success": job["status"] == "completed" and job["candidate_id"] == candidate["id"] and candidate["status"] == "pending"
                               and original == path.read_bytes() and len(history) == 2 and len(requests) == 2
                               and requests[1]["background_thread"] and not requests[1]["tools_provided"] and not errors.getvalue()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    report = run_check(live=args.live)
    path = ROOT / f"codex/skill-background-{'live-' if args.live else ''}check-2026-10-28.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"report": str(path), "model": report["model"], "job_status": report["job"]["status"], "success": report["success"]}, ensure_ascii=False))
    if not report["success"]:
        raise SystemExit(1)
