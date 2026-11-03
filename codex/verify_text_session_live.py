"""启动两个独立 CLI 进程，真实验证文本会话恢复；会发送两次模型请求。"""

import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parent.parent
# 直接运行 codex 下的脚本时，也从当前项目导入包。
sys.path.insert(0, str(ROOT))

from personal_agent.config import load_model_config
from personal_agent.session import TextSession


def run_cli(option: str, name: str, prompt: str) -> dict:
    """等待一个实际 CLI 进程完成，返回 PID、退出码和终端输出。"""
    command = [sys.executable, "-X", "utf8", "-B", "-m", "personal_agent.main", option, name]
    with subprocess.Popen(
        command, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ) as process:
        try:
            stdout, stderr = process.communicate(prompt + "\nexit\n", timeout=75)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise RuntimeError("文本会话 CLI 验证超时。") from None
        return {"pid": process.pid, "exit_code": process.returncode, "stdout": stdout, "stderr": stderr}


def main() -> None:
    name = f"live_{uuid4().hex[:8]}"
    token = f"松果-{uuid4().int % 1000000:06d}"
    first_prompt = f"请记住本次会话的练习口令：{token}。请只回复：已记住。"
    second_prompt = "上一段对话里我让你记住的练习口令是什么？请只输出口令，不要解释。"
    session = TextSession(name)
    first = run_cli("--session", name, first_prompt)
    first_messages = session.load()
    second = run_cli("--resume", name, second_prompt) if len(first_messages) == 2 else None
    final_messages = session.load()
    success = (
        second is not None and first["exit_code"] == second["exit_code"] == 0
        and not first["stderr"] and not second["stderr"]
        and len(final_messages) == 4 and final_messages[:2] == first_messages
        and final_messages[2] == {"role": "user", "content": second_prompt}
        and final_messages[3]["content"].strip() == token
    )
    report = {
        "validation": "Two sequential, independent CLI processes; real model requests and JSON persistence",
        "model": load_model_config()["model"], "session_name": name,
        "session_file": session.path.relative_to(ROOT).as_posix(),
        "first_prompt": first_prompt, "second_prompt": second_prompt,
        "token": token, "second_prompt_contains_token": token in second_prompt,
        "first_process": first, "second_process": second,
        "saved_messages_after_first_exit": len(first_messages), "saved_messages_after_resume_exit": len(final_messages),
        "previous_history_preserved": final_messages[:2] == first_messages,
        "final_reply": final_messages[-1]["content"] if final_messages else None,
        "success": success,
    }
    report_path = ROOT / "codex/text-session-live-check-2026-10-26.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not success:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
