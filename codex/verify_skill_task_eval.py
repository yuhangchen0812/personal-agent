"""隔离文件任务回放；--live 由真实模型使用文件工具，候选和源反馈固定，不采纳。"""

import argparse
import hashlib
import io
import json
import re
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
from personal_agent.skill_eval import SkillEvaluator
from personal_agent.skill_evolution import SkillEvolutionStore
from personal_agent.skills import create_project_skill


def tool(name, arguments, call_id):
    return {"role": "assistant", "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]}


def run_check(*, live=False):
    config = load_model_config() if live else None
    output, errors, requests = io.StringIO(), io.StringIO(), []
    with tempfile.TemporaryDirectory(prefix="personal-agent-tool-eval-") as temporary:
        root = Path(temporary).resolve()
        with patch("personal_agent.skills.PROJECT_ROOT", root), patch("personal_agent.skills.USER_SKILLS_ROOT", root / "isolated-user-skills"), patch("personal_agent.main.get_model_settings", return_value={key: value for key, value in config.items() if key != "api_key"} if config else {"protocol": "openai", "model": "scripted", "base_url": "https://example.invalid"}):
            path = create_project_skill("file-summary", "完成文件统计", "依据任务要求完成工作：$ARGUMENTS")
            original = path.read_bytes()
            store = SkillEvolutionStore()
            feedback = store.record_feedback("file-summary", "文件统计需要实际读取、写入和核对。", [
                {"role": "user", "content": "说明一种通用文件处理习惯。"}, {"role": "assistant", "content": "固定源回答：直接概括结果。"}])
            candidate = store.draft(feedback["id"], lambda _: {"role": "assistant", "content": json.dumps({
                "lesson": "处理文件统计任务时，先用 read_file 读取实际材料并逐项核算，再用 write_file 写出要求的 JSON 文件，写完用 read_file 读回验证，最后简洁说明位置和依据。",
                "rationale": "固定手工候选，用于演示工具任务回放。"}, ensure_ascii=False)})
            candidate_raw = store._path("candidate", candidate["id"]).read_bytes()
            suite_path = root / "file-suite.json"
            suite_path.write_bytes((ROOT / "examples/evals/file-summary-holdout.json").read_bytes())

            def model(messages, definitions=None, *, on_usage, **options):
                entry = {"messages_count": len(messages), "tools": [definition["name"] for definition in definitions], "options": options}
                requests.append(entry)
                def capture(usage):
                    entry["usage"] = usage
                    on_usage(usage)
                if live:
                    return get_model_message(messages, definitions, on_usage=capture, **options)
                capture({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                if len(messages) == 2:
                    return tool("read_file", {"file_path": "playground/batches.json"}, "read-input")
                if messages[-1].get("tool_call_id") == "read-input":
                    text = re.sub(r"(?m)^[ \t]*\d+ \| ?", "", messages[-1]["content"])
                    data = json.loads(text)
                    total = sum(item["count"] for item in data["batches"])
                    # 固定模拟对原版故意少计 1；只证明产物验收分支，不代表模型效果。
                    if "候选补充经验" not in messages[0]["content"]:
                        total -= 1
                    return tool("write_file", {"file_path": "playground/summary.json", "content": json.dumps({"total_count": total, "batch_count": len(data["batches"])})}, "write-summary")
                if messages[-1].get("tool_call_id") == "write-summary" and "候选补充经验" in messages[0]["content"]:
                    return tool("read_file", {"file_path": "playground/summary.json"}, "read-back")
                return {"role": "assistant", "content": "结果文件：playground/summary.json；以实际工具返回和本地验收为准。"}

            evaluator = SkillEvaluator()
            def inputs():
                yield f"/skill-eval-tools {candidate['id']} {suite_path}"
                report = evaluator.list_reports()[0]
                yield f"/skill-evals {report['id']}"
                yield "/usage"
                yield "exit"

            with patch("personal_agent.main.get_model_message", side_effect=model), patch("builtins.input", side_effect=inputs()), redirect_stdout(output), redirect_stderr(errors):
                history = run_repl(permission_mode="acceptEdits", max_turns=5)
            report = evaluator.list_reports()[0]
            artifacts = []
            for row in report["rows"]:
                target = root / row["workspace"] / "playground/summary.json" if "workspace" in row else None
                artifacts.append({"variant": row["variant"], "workspace": row.get("workspace"), "result": json.loads(target.read_text(encoding="utf-8")) if target is not None and target.exists() else None})
            unchanged = original == path.read_bytes() and candidate_raw == store._path("candidate", candidate["id"]).read_bytes()
            return {"validation": "live model requests and real file tools, fixed source feedback/candidate and authored fixture" if live else "fixed responses and usage, real file tools; baseline deliberately miscounts; no network",
                    "model": config["model"] if live else "scripted", "requests": requests, "evaluation": report, "artifacts": artifacts,
                    "active_skill_unchanged": unchanged, "candidate_pending": store.candidate(candidate["id"])["status"] == "pending",
                    "original_sha256": hashlib.sha256(original).hexdigest(), "history_messages": len(history), "stderr": errors.getvalue(),
                    "boundary": "One authored file task, one pair, file-artifact checks; no semantic-quality, generalization, full shell/MCP task or cost-improvement claim. Temporary workspaces removed after verification.",
                    "success": report["status"] == "complete" and report["candidate_unchanged"] and unchanged and history == [] and not errors.getvalue()
                               and all(row["trace"] and row["files_after"] for row in report["rows"])
                               and all(entry["tools"] for entry in requests)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    report = run_check(live=args.live)
    path = ROOT / f"codex/skill-task-eval-{'live-' if args.live else ''}check-2026-10-29.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"report": str(path), "model_requests": len(report["requests"]), "decision": report["evaluation"]["decision"], "success": report["success"]}, ensure_ascii=False))
    if not report["success"]:
        raise SystemExit(1)
