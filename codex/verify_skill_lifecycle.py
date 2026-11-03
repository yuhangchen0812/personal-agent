"""固定模型配合实际 CLI/文件系统，验证 Skill 事件与整目录归档/恢复；无网络请求。"""

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

from personal_agent.main import run_repl
from personal_agent.skill_lifecycle import SkillLifecycleStore
from personal_agent.skills import create_project_skill, discover_skills, load_skill


def fingerprints(directory):
    return {path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(directory.rglob("*")) if path.is_file()}


def run_check():
    requests, observed = [], {}
    output, errors = io.StringIO(), io.StringIO()
    with tempfile.TemporaryDirectory(prefix="personal-agent-lifecycle-") as temporary:
        root = Path(temporary).resolve()
        with patch("personal_agent.skills.PROJECT_ROOT", root), patch("personal_agent.skills.USER_SKILLS_ROOT", root / "isolated-user-skills"):
            path = create_project_skill("review", "检查函数参数", "解释给出的函数参数，不编造执行结果：$ARGUMENTS")
            asset = path.parent / "资料/示例.txt"
            asset.parent.mkdir()
            asset.write_bytes("附带资源\r\n".encode("utf-8"))
            fork = create_project_skill("branch", "独立处理", "独立说明任务：$ARGUMENTS")
            fork.write_text(fork.read_text(encoding="utf-8").replace('context: "inline"', 'context: "fork"'), encoding="utf-8")
            original = fingerprints(path.parent)
            store = SkillLifecycleStore()

            def model(messages, definitions=None, *, on_usage, **options):
                requests.append({"roles": [message["role"] for message in messages], "tools_provided": bool(definitions)})
                on_usage({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                if any("skill-fork:" in message.get("content", "") for message in messages if message["role"] == "system"):
                    return {"role": "assistant", "content": "固定 fork 返回文本，不代表真实任务质量。"}
                last = messages[-1]
                if last["role"] == "user" and last["content"].startswith("检查函数参数"):
                    return {"role": "assistant", "tool_calls": [{"id": "load", "type": "function", "function": {"name": "skill", "arguments": '{"skill_name":"review"}'}}]}
                return {"role": "assistant", "content": "固定 inline 回答，不代表真实模型效果。"}

            def inputs():
                yield "/skills search 检查函数参数"
                observed["search_did_not_create_events"] = not (root / ".personal/skill-usage").exists()
                yield "检查函数参数"
                yield "/review 显式调用"
                yield "/branch 独立任务"
                yield "/skill-stats"
                observed["statistics_before_archive"] = store.statistics()
                before_local = len(requests)
                yield "/skill-archive review 暂时整理课程技能"
                record = store.list_archives()[0]
                payload = store._archive_path(record["id"]) / "skill"
                observed["archive"] = record
                observed["archive_preserved_all_file_hashes"] = fingerprints(payload) == original
                observed["archived_name_not_discovered"] = "review" not in [skill.name for skill in discover_skills()[0]]
                observed["original_directory_absent"] = not path.parent.exists()
                try:
                    load_skill(payload / "SKILL.md")
                except ValueError as error:
                    observed["archive_path_loading_rejected"] = str(error)
                yield "/review 已归档的调用"
                yield "/skill-archives"
                yield f"/skill-restore {record['id']}"
                yield f"/skill-restore {record['id']}"
                yield "/skill-stats review"
                observed["local_commands_model_calls"] = len(requests) - before_local
                observed["restored_file_hashes"] = fingerprints(path.parent)
                observed["restored_record"] = store.archive_record(record["id"])
                observed["statistics_after_restore"] = SkillLifecycleStore().statistics()
                observed["restored_name_discovered"] = "review" in [skill.name for skill in discover_skills()[0]]
                yield "exit"

            with patch("personal_agent.main.get_model_message", side_effect=model), patch("builtins.input", side_effect=inputs()), redirect_stdout(output), redirect_stderr(errors):
                history = run_repl(permission_mode="plan")
            review = next(row for row in observed["statistics_after_restore"] if row["name"] == "review")
            branch = next(row for row in observed["statistics_after_restore"] if row["name"] == "branch")
            return {"validation": "fixed model, real CLI/skill tools/filesystem/events/archive/restore; no network requests",
                    "project_root": str(root), "requests": requests, "original_file_hashes": original, **observed,
                    "history_messages": len(history), "stderr": errors.getvalue(),
                    "boundary": "Counts observe runtime events, not success, relevance or skill improvement; logs exclude task/argument bodies; temporary project removed after verification.",
                    "success": all([observed["search_did_not_create_events"], observed["archive_preserved_all_file_hashes"],
                                    observed["archived_name_not_discovered"], observed["original_directory_absent"],
                                    bool(observed.get("archive_path_loading_rejected")), observed["local_commands_model_calls"] == 0,
                                    observed["restored_file_hashes"] == original, observed["restored_name_discovered"],
                                    review["counts"]["inline_loaded"] == 2, branch["counts"]["fork_started"] == 1,
                                    branch["counts"]["fork_returned"] == 1, observed["restored_record"]["status"] == "restored"])}


if __name__ == "__main__":
    report = run_check()
    target = ROOT / "codex/skill-lifecycle-check-2026-10-28.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"report": str(target), "fixed_model_calls": len(report["requests"]), "success": report["success"]}, ensure_ascii=False))
    if not report["success"]:
        raise SystemExit(1)
