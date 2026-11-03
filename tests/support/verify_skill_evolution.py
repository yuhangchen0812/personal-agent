"""临时项目的反馈 -> 模型草稿 -> 查看 -> 显式采纳；--live 仅请求一次真实模型。"""

import argparse
import hashlib
import io
import json
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from personal_agent.config import load_model_config
from personal_agent.llm import get_model_message
from personal_agent.main import run_repl
from personal_agent.skill_evolution import SkillEvolutionStore
from personal_agent.skills import create_project_skill, load_inline_skill


def run_check(*, live=False):
    config = load_model_config() if live else None
    requests, steps = [], []
    output, errors = io.StringIO(), io.StringIO()
    with tempfile.TemporaryDirectory(prefix="personal-agent-skill-feedback-") as temporary:
        root = Path(temporary).resolve()
        with patch("personal_agent.skills.PROJECT_ROOT", root), \
             patch("personal_agent.skills.USER_SKILLS_ROOT", root / "isolated-user-skills"), \
             patch("personal_agent.main.get_model_settings", return_value={key: value for key, value in config.items() if key != "api_key"} if config else {"protocol": "openai", "model": "scripted", "base_url": "https://example.invalid"}):
            path = create_project_skill("explain-flow", "解释 Python 函数", "讲解 $ARGUMENTS 中的函数。")
            before = path.read_bytes()
            store = SkillEvolutionStore()
            fixture = [{"role": "user", "content": "解释 read_file 的参数、返回值和调用关系。"},
                       {"role": "assistant", "content": "read_file 用于读取文件。"}]
            feedback = store.record_feedback("explain-flow", "回答遗漏参数含义、返回值结构和调用顺序。补充通用讲解步骤，区分真实执行和推测，不编造函数细节。", fixture)

            def model(messages, definitions=None, *, on_usage, **options):
                requests.append({"message_count": len(messages), "tools_provided": bool(definitions)})

                def capture(usage):
                    requests[-1]["usage"] = usage
                    on_usage(usage)

                if live:
                    return get_model_message(messages, definitions, on_usage=capture, **options)
                capture({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                return {"role": "assistant", "content": json.dumps({
                    "lesson": "解释函数时，依次说明参数含义、返回值结构和调用顺序；引用实际证据，证据不足时明确标注待核实。",
                    "rationale": "用户反馈指出原回答只说明用途，遗漏了参数、返回值和调用关系。",
                }, ensure_ascii=False)}

            def inputs():
                yield f"/skill-draft {feedback['id']}"
                candidate = store.list_records("candidate")[0]
                assert path.read_bytes() == before
                steps.append("draft_saved_active_skill_unchanged")
                yield f"/skill-candidates {candidate['id']}"
                # 只向本次自行创建的临时项目写入经验，不修改用户当前项目的活动技能。
                yield f"/skill-adopt {candidate['id']}"
                applied = path.read_bytes()
                yield f"/skill-adopt {candidate['id']}"
                assert path.read_bytes() == applied
                steps.append("duplicate_adoption_did_not_append_again")
                yield "/usage"
                yield "exit"

            with patch("personal_agent.main.get_model_message", side_effect=model), \
                 patch("builtins.input", side_effect=inputs()), redirect_stdout(output), redirect_stderr(errors):
                history = run_repl(permission_mode="plan")
            candidate = SkillEvolutionStore().list_records("candidate")[0]
            snapshot = root / candidate["snapshot"]
            after = path.read_bytes()
            future = load_inline_skill("explain-flow", "demo.py")
            success = (
                len(requests) == 1 and not requests[0]["tools_provided"] and history == []
                and candidate["status"] == "adopted" and snapshot.read_bytes() == before
                and after.startswith(before) and after.count(candidate["id"].encode()) == 1
                and candidate["lesson"] in future and not errors.getvalue()
            )
            return {
                "validation": "one live model draft request with fixed source task/result" if live else "fixed source task/result, model and usage; no network request",
                "actual_components": "isolated temporary project, run_repl maintenance commands, feedback/candidate files, review, adoption, raw snapshot, duplicate guard and future inline loading",
                "model": config["model"] if config else "scripted", "api_key_present": bool(config["api_key"]) if config else None,
                "feedback": feedback, "candidate": candidate, "requests": requests, "steps": steps,
                "original_text": before.decode("utf-8"), "updated_text": after.decode("utf-8"),
                "original_sha256": hashlib.sha256(before).hexdigest(), "snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                "future_skill_contains_lesson": candidate["lesson"] in future,
                "conversation_messages_added_by_commands": len(history), "stdout": output.getvalue(), "stderr": errors.getvalue(),
                "evaluation_boundary": "No task-quality evaluation, original task live run, background learning, model-weight change or user-project skill modification",
                "success": success,
            }
