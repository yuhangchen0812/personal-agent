"""固定模型驱动实际 Agent Loop、权限与文件产物；输出持久化且可重验。"""

from copy import deepcopy
from html import escape
import json
from pathlib import Path
import re
from uuid import uuid4

from .agent import run_agent_loop
from .file_runtime import plain_path, write_json_atomic
from .plan import PlanSession
from .session import validate_agent_messages
from .source_snapshot import digest
from .tool_registry import TOOL_ERROR_PREFIX, execute_tool
from .workspace import tool_workspace


INPUT = b'{"numbers": [2, 3, 5]}\n'
PLAN = "读取本演示输入，批准后写入总和与计数，再读回核对。"
SUMMARY = json.dumps({"total": 10, "count": 3}, ensure_ascii=False, indent=2) + "\n"
FILES = ("workspace/playground/input.json", "workspace/playground/summary.json", "transcript.json", "trace.json", "approvals.json")
TOOLS = {"read_file", "write_file", "enter_plan_mode", "exit_plan_mode"}


def _call(index, name, arguments):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": f"demo-{index}", "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]}


class OfflineDemoModel:
    def __init__(self):
        self.calls = 0
        self.plan_file = None

    def __call__(self, messages, definitions):
        step = self.calls
        self.calls += 1
        if step == 0:
            return _call(step, "read_file", {"file_path": "playground/input.json"})
        if step == 1:
            return _call(step, "enter_plan_mode", {})
        if step == 2:
            self.plan_file = re.search(r"playground/\.plans/plan-[a-f0-9]{32}\.md", messages[-1]["content"]).group()
            return _call(step, "write_file", {"file_path": self.plan_file, "content": PLAN})
        if step == 3:
            return _call(step, "write_file", {"file_path": "playground/summary.json", "content": SUMMARY})
        if step == 4:
            return _call(step, "exit_plan_mode", {})
        if step == 5:
            return _call(step, "write_file", {"file_path": "playground/summary.json", "content": SUMMARY})
        if step == 6:
            return _call(step, "read_file", {"file_path": "playground/summary.json"})
        return {"role": "assistant", "content": "离线演示已完成：Plan 前写入被拒绝，批准后生成并读回汇总。模型响应为固定脚本。"}


def _measure(directory):
    inputs = (directory / FILES[0]).read_bytes()
    summary = json.loads((directory / FILES[1]).read_text(encoding="utf-8"))
    messages = validate_agent_messages(json.loads((directory / "transcript.json").read_text(encoding="utf-8")))
    trace = json.loads((directory / "trace.json").read_text(encoding="utf-8"))
    approvals = json.loads((directory / "approvals.json").read_text(encoding="utf-8"))
    calls = [call for message in messages for call in message.get("tool_calls", [])]
    results = [message for message in messages if message["role"] == "tool"]
    paired = len(calls) == len(results) == len(trace) == 7 and all(
        row["name"] == call["function"]["name"] and row["arguments"] == json.loads(call["function"]["arguments"])
        and row["result"] == result["content"] and result["tool_call_id"] == call["id"] for row, call, result in zip(trace, calls, results))
    early = trace[3] if paired else {}
    plan = trace[2].get("arguments", {}) if paired else {}
    plan_name = plan.get("file_path", "")
    plan_matches = (paired and trace[2]["name"] == "write_file" and plan.get("content") == PLAN
        and re.fullmatch(r"playground/\.plans/plan-[a-f0-9]{32}\.md", plan_name) is not None
        and plain_path(directory / "workspace" / plan_name).read_bytes() == PLAN.encode("utf-8"))
    return {"input_unchanged": inputs == INPUT,
        "actual_artifact_matches_input": summary == {"total": sum(json.loads(inputs)["numbers"]), "count": len(json.loads(inputs)["numbers"])},
        "tool_calls_and_results_paired": paired,
        "plan_write_denied_before_approval": early.get("name") == "write_file" and early.get("result", "").startswith(TOOL_ERROR_PREFIX)
            and early.get("artifact_exists_after") is False,
        "explicit_fixed_demo_approvals": approvals == ["exit_plan_mode", "write_file"],
        "actual_plan_matches_approved_text": bool(plan_matches),
        "approved_write_and_actual_readback": paired and trace[5]["name"] == "write_file" and trace[5]["artifact_exists_after"] is True
            and trace[6]["name"] == "read_file" and '"total": 10' in trace[6]["result"]}


def _html(report):
    rows = "".join("<li>" + escape(key) + "：" + ("通过" if value else "失败") + "</li>" for key, value in report["checks"].items())
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Personal Agent 离线演示</title>'
        '<style>body{font-family:system-ui;max-width:850px;margin:40px auto;padding:0 20px;line-height:1.8}code{background:#eee;padding:3px}</style>'
        '<h1>Personal Agent 离线演示</h1><p>固定模型响应；文件工具、Plan 和审批实际执行。无需密钥，远端请求为 0。</p>'
        '<p>状态：<strong>' + escape(report["status"]) + '</strong>。这是机制演示，不是模型修复质量评测。</p><ul>' + rows + '</ul>'
        '<p><a href="workspace/playground/summary.json">查看汇总产物</a> · <a href="trace.json">工具轨迹</a> · '
        '<a href="transcript.json">完整调用记录</a> · <a href="report.json">报告及文件指纹</a></p></html>')


def run_demo(output_dir=None):
    directory = plain_path(output_dir if output_dir is not None else Path(__file__).resolve().parent.parent / ".personal/delivery-demos" / uuid4().hex)
    if directory.exists():
        raise ValueError("演示输出须为新目录，已有产物不覆盖。")
    workspace = directory / "workspace"
    (workspace / "playground").mkdir(parents=True, exist_ok=False)
    (workspace / "playground/input.json").write_bytes(INPUT)
    trace, approvals = [], []
    model, plan = OfflineDemoModel(), PlanSession()
    messages = [{"role": "user", "content": "运行预先审阅的离线固定流程：读输入，进入规划，验证未批准写入被拒绝，批准后写入并读回汇总。"}]
    report = {"version": 1, "kind": "offline_delivery_demo", "directory": str(directory), "status": "running",
        "validation": "fixed model; actual Agent Loop, file tools and Plan; fixed approvals only for this authored fixture",
        "new_remote_model_calls": 0, "checks": {}}
    write_json_atomic(directory / "report.json", report)

    def approve(name, arguments, reason):
        valid = (name == "exit_plan_mode" and arguments["plan"] == PLAN) or (name == "write_file"
            and arguments == {"file_path": "playground/summary.json", "content": SUMMARY})
        if valid:
            approvals.append(name)
        return valid

    def execute(name, arguments):
        with tool_workspace(workspace):
            result = execute_tool(name, arguments, plan=plan, confirm=approve, deny=["run_shell"]) if name in TOOLS else TOOL_ERROR_PREFIX + "演示不提供该工具。"
        trace.append({"name": name, "arguments": deepcopy(arguments), "result": result,
            "artifact_exists_after": (workspace / "playground/summary.json").exists()})
        return result

    try:
        run_agent_loop(messages, model, max_turns=9, tool_executor=execute)
        write_json_atomic(directory / "transcript.json", validate_agent_messages(messages))
        write_json_atomic(directory / "trace.json", trace)
        write_json_atomic(directory / "approvals.json", approvals)
        report["checks"] = _measure(directory)
        report["status"] = "passed" if all(report["checks"].values()) else "failed"
        report["scripted_model_calls"] = model.calls
        report["artifacts"] = {name: digest((directory / name).read_bytes()) for name in FILES}
    except (Exception, KeyboardInterrupt) as error:
        report.update(status="cancelled" if isinstance(error, KeyboardInterrupt) else "failed", error_type=type(error).__name__)
    write_json_atomic(directory / "report.json", report)
    (directory / "index.html").write_text(_html(report), encoding="utf-8")
    return {"status": report["status"], "validation": report["validation"], "report": str(directory / "report.json"),
        "html": str(directory / "index.html"), "checks": report["checks"], "new_remote_model_calls": 0}


def check_demo(report):
    path = plain_path(report)
    value = json.loads(path.read_text(encoding="utf-8"))
    directory = path.parent
    if (path.name != "report.json" or value.get("version") != 1 or value.get("kind") != "offline_delivery_demo"
            or value.get("directory") != str(directory) or value.get("status") != "passed"
            or set(value.get("artifacts", {})) != set(FILES)):
        raise ValueError("演示报告格式、位置或完成状态无效。")
    for name, sha in value["artifacts"].items():
        if digest(plain_path(directory / name).read_bytes()) != sha:
            raise ValueError("演示产物或轨迹变化。")
    if _measure(directory) != value["checks"] or not all(value["checks"].values()) or (directory / "index.html").read_text(encoding="utf-8") != _html(value):
        raise ValueError("演示检查或展示内容与实际产物不一致。")
    return {"status": "passed", "report": str(path), "checks": value["checks"], "validation": "read-only artifact recheck; no tools or models replayed", "new_remote_model_calls": 0}
