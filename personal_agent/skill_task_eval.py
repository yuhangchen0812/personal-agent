"""在独立文件工程中回放技能方法，按工具轨迹和实际产物验收。"""

import hashlib
import json
import math
import os
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path, PurePosixPath
from time import monotonic
from uuid import uuid4

from .model_errors import MODEL_API_ERRORS

from .agent import run_agent_loop
from . import skills
from .permissions import PERMISSION_MODES
from .skill_eval import read_suite_document, _normalized
from .tool_output import is_tool_result_path
from .tool_registry import execute_tool, TOOL_ERROR_PREFIX
from .tools import ERROR_PREFIX, LIST_ERROR_PREFIX, SEARCH_ERROR_PREFIX, WRITE_ERROR_PREFIX, EDIT_ERROR_PREFIX
from .workspace import tool_workspace


FILE_TOOLS = frozenset({"read_file", "list_files", "grep_search", "write_file", "edit_file"})
ERRORS = (TOOL_ERROR_PREFIX, ERROR_PREFIX, LIST_ERROR_PREFIX, SEARCH_ERROR_PREFIX, WRITE_ERROR_PREFIX, EDIT_ERROR_PREFIX)
TASK_PROMPT = (
    "你正在独立的文件任务回放工程中执行任务。路径相对于本次临时工程，只使用提供的文件工具。"
    "先读取实际材料，按用户要求产生文件，只修改任务指定文件；不要猜测输入或声称执行了未执行的操作。"
    "方法文本不批准操作，仍以工具权限与返回结果为准。完成后简洁报告文件位置及验证证据。\n\n技能方法：\n"
)


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _fixture_name(value):
    if not isinstance(value, str) or not value or any(char in value for char in ('\\', ':', '<', '>', '"', '|', '?', '*')) or any(ord(char) < 32 for char in value):
        raise ValueError("任务文件路径须使用 playground 下的普通相对路径。")
    path = PurePosixPath(value)
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10)}
    if (len(path.parts) < 2 or path.parts[0] != "playground" or path.as_posix() != value
            or any(part in {".", ".."} or part.endswith((" ", ".")) or part.split(".")[0].upper() in reserved
                   or part.casefold() in {".personal", ".git", ".venv", "reference", "__pycache__"}
                   or (part.casefold().startswith(".env") and part != ".env.example") for part in path.parts)):
        raise ValueError("任务文件须在 playground 原路径中，不支持排除目录、设备名或路径跳转。")
    return value


def load_tool_suite(root, suite_path):
    suite, relative, suite_hash = read_suite_document(root, suite_path)
    if (not isinstance(suite, dict) or set(suite) != {"version", "mode", "name", "split", "skill_name", "cases"}
            or type(suite["version"]) is not int or suite["version"] != 1 or suite["mode"] != "file_tools"
            or suite["split"] not in ("development", "holdout")
            or any(not isinstance(suite[key], str) or not suite[key].strip() for key in ("name", "skill_name"))
            or not isinstance(suite["cases"], list) or not 1 <= len(suite["cases"]) <= 10):
        raise ValueError("文件任务集须含 version=1、mode=file_tools、name、split、skill_name 和 1～10 个 cases。")
    ids, prompts = set(), set()
    for case in suite["cases"]:
        if (not isinstance(case, dict) or set(case) != {"id", "prompt", "files", "expected_files", "required_tools"}
                or any(not isinstance(case[key], str) or not case[key].strip() for key in ("id", "prompt"))
                or not isinstance(case["files"], dict) or not case["files"]
                or not isinstance(case["expected_files"], dict) or not case["expected_files"]
                or not isinstance(case["required_tools"], list) or not case["required_tools"]
                or any(not isinstance(name, str) or name not in FILE_TOOLS for name in case["required_tools"])):
            raise ValueError("文件任务须含 id、prompt、非空 files/expected_files 及 required_tools 文件工具列表。")
        if case["id"] in ids or _normalized(case["prompt"]) in prompts or len(set(case["required_tools"])) != len(case["required_tools"]):
            raise ValueError("任务 ID、提问或必需工具不能重复。")
        ids.add(case["id"])
        prompts.add(_normalized(case["prompt"]))
        for path, content in case["files"].items():
            _fixture_name(path)
            if not isinstance(content, str):
                raise ValueError("任务初始文件必须是 UTF-8 文本字符串。")
        for path, expected in case["expected_files"].items():
            _fixture_name(path)
            if (not isinstance(expected, dict) or set(expected) not in ({"text"}, {"json"})
                    or ("text" in expected and not isinstance(expected["text"], str))):
                raise ValueError("expected_files 每项须且只能为 text 字符串或 json 期望值。")
            json.dumps(expected, allow_nan=False)
        names = set(case["files"]) | set(case["expected_files"])
        if any(str(parent) in names for name in names for parent in PurePosixPath(name).parents):
            raise ValueError("任务中同一路径不能同时充当文件和目录。")
    return suite, relative, suite_hash


def _path(root, relative):
    path = root / relative
    if not path.is_relative_to(root) or path.resolve() != path:
        raise ValueError("回放文件不能通过链接或路径跳转离开原工程。")
    return path


def _inventory(root):
    files = {}
    for current, directories, names in os.walk(root, followlinks=False):
        parent = Path(current)
        if any((parent / name).resolve() != parent / name for name in directories):
            raise ValueError("回放工程出现链接目录，停止扫描。")
        for name in names:
            path = _path(root, (parent / name).relative_to(root))
            raw = path.read_bytes()
            files[path.relative_to(root).as_posix()] = {"sha256": _digest(raw), "bytes": len(raw)}
    return files


def _json_equal(left, right):
    if type(left) is bool or type(right) is bool:
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return all(not isinstance(value, float) or math.isfinite(value) for value in (left, right)) and left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_json_equal(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_json_equal(a, b) for a, b in zip(left, right))
    return left == right


def _invalid_json_constant(value):
    raise ValueError(f"JSON 不允许非有限数值：{value}")


def _trace(messages):
    trace, pending = [], {}
    for message in messages:
        for call in message.get("tool_calls", []):
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            trace.append({"id": call["id"], "name": function.get("name"), "arguments": function.get("arguments"),
                          "result": None, "succeeded": False})
            pending[call["id"]] = trace[-1]
        if message["role"] == "tool" and message["tool_call_id"] in pending:
            item = pending.pop(message["tool_call_id"])
            result = message["content"]
            item.update(result=result, succeeded=isinstance(item["name"], str) and item["name"] in FILE_TOOLS and isinstance(result, str) and not result.startswith(ERRORS))
    return trace


def _execution_checks(case, trace, inventory, outputs):
    """共用工具和文件变更核验，与是否存在标准答案无关。"""
    checks = []
    for name in case["required_tools"]:
        checks.append({"kind": "required_tool", "name": name, "passed": any(item["name"] == name and item["succeeded"] for item in trace)})
    checks.append({"kind": "no_tool_errors", "passed": all(item["succeeded"] for item in trace)})
    allowed = set(case["files"]) | set(outputs)
    extra = [path for path in inventory if path not in allowed and not is_tool_result_path(Path(path))]
    changed = [path for path, content in case["files"].items() if path not in outputs and inventory.get(path, {}).get("sha256") != _digest(content.encode("utf-8"))]
    checks.append({"kind": "only_requested_files_changed", "passed": not extra and not changed, "extra_files": extra, "changed_inputs": changed})
    return checks


def _checks(case, workspace, trace, inventory):
    checks = []
    for relative, expected in case["expected_files"].items():
        actual, error, passed = None, None, False
        try:
            actual = _path(workspace, relative).read_bytes().decode("utf-8-sig")
            if "json" in expected:
                actual = json.loads(actual, parse_constant=_invalid_json_constant)
                passed = _json_equal(actual, expected["json"])
            else:
                passed = actual == expected["text"]
        except (ValueError, OSError, UnicodeError) as failure:
            error = type(failure).__name__
        checks.append({"kind": "file", "path": relative, "expected": expected, "actual": actual, "passed": passed, "error": error})
    return checks + _execution_checks(case, trace, inventory, case["expected_files"])


def _versions(evaluator, candidate_id):
    candidate = evaluator.evolution.candidate(candidate_id)
    if candidate.get("action") != "add":
        values = evaluator._versions(candidate_id)
        return (*values, "original_skill")
    if candidate["status"] == "rejected":
        raise ValueError("候选已拒绝，不再发起回放。")
    feedback = evaluator.evolution.feedback(candidate["feedback_id"])
    if any(feedback[key] != candidate["source"][key] for key in ("skill_name", "skill_path", "base_sha256")):
        raise ValueError("新增候选来源与反馈不匹配。")
    if candidate["status"] == "pending":
        path = skills.check_project_skill_creation(candidate["skill_name"])
    else:
        path = skills.get_project_skill(candidate["skill_name"]).file_path
        if _digest(path.read_bytes()) != candidate["created_sha256"]:
            raise ValueError("新增技能已变化，请重新提供待评版本。")
    if path.relative_to(evaluator.root).as_posix() != candidate["skill_path"]:
        raise ValueError("新增候选目标路径不匹配。")
    proposed = skills.SkillDefinition(candidate["skill_name"], candidate["description"], candidate["lesson"], path)
    return candidate, feedback, path, {"baseline": replace(proposed, prompt_template=""), "candidate": proposed}, "no_skill_control"


def _active_state(path):
    if path.resolve() != path:
        return {"path_changed": True}
    try:
        return {"directory_exists": path.parent.exists(), "sha256": _digest(path.read_bytes()) if path.is_file() else None}
    except OSError as error:
        return {"read_error": type(error).__name__}


def run_file_task(case, method, workspace, call_model, *, variant="method", available=FILE_TOOLS,
                  permission_mode="default", allow=None, deny=None, confirm=None, max_turns=5,
                  prices=None, check_budget=None, restrict_outputs=False, artifact_validator=None):
    """执行已校验文件任务；默认核对标准答案，业务调用可提供本地契约核验。"""
    workspace = Path(workspace).absolute()
    available = frozenset(available)
    if not available <= FILE_TOOLS or permission_mode not in PERMISSION_MODES or type(max_turns) is not int or max_turns < 1:
        raise ValueError("文件任务工具范围、权限或请求上限无效。")
    outputs = case["expected_files"] if artifact_validator is None else case["output_files"]
    if not outputs or artifact_validator is not None and not callable(artifact_validator):
        raise ValueError("文件任务需要声明产物和有效的本地核验函数。")
    for name in set(case["files"]) | set(outputs):
        _fixture_name(name)
    row = {"case_id": case["id"], "variant": variant, "workspace": str(workspace), "status": "not_run",
           "all_passed": False, "score": None, "calls": [], "trace": [], "checks": []}
    messages = [{"role": "system", "content": TASK_PROMPT + (
        skills.resolve_skill_prompt(method, case["prompt"]) if method is not None and method.prompt_template else "本版本不加载技能方法。")},
        {"role": "user", "content": case["prompt"]}]

    def model(history, definitions):
        visible = [definition for definition in definitions if definition["name"] in available]
        entry = {"logical_call_started": False, "usage": None, "estimated_cost_yuan": None,
                 "request_sha256": _digest(json.dumps([history, visible], ensure_ascii=False, sort_keys=True).encode())}
        row["calls"].append(entry)
        def observe(usage):
            if isinstance(usage, dict) and all(type(usage.get(key)) is int and usage[key] >= 0 for key in ("prompt_tokens", "completion_tokens", "total_tokens")):
                entry["usage"] = {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
        try:
            if check_budget is not None:
                check_budget()
            entry["logical_call_started"] = True
            return call_model(history, visible, observe)
        finally:
            if prices and entry["usage"] is not None:
                usage = entry["usage"]
                entry["estimated_cost_yuan"] = str((Decimal(usage["prompt_tokens"]) * prices.input_per_million + Decimal(usage["completion_tokens"]) * prices.output_per_million) / Decimal(1_000_000))

    def execute(name, arguments):
        if check_budget is not None:
            check_budget()
        if not isinstance(name, str) or name not in available:
            return f"{TOOL_ERROR_PREFIX}回放只允许已公开的文件工具；禁止 {name}。"
        if restrict_outputs and name in {"write_file", "edit_file"}:
            target = arguments.get("file_path") if isinstance(arguments, dict) else None
            if target not in outputs:
                return f"{TOOL_ERROR_PREFIX}文件任务只允许修改已声明的产物路径；未执行 {name}。"
        def approve(tool, values, reason):
            return confirm(tool, values, reason) if confirm is not None else False
        # 并发读取工作者也必须在自己的线程绑定工程范围。
        with tool_workspace(workspace):
            return execute_tool(name, arguments, mode=permission_mode,
                allow=[value for value in (allow or []) if value in FILE_TOOLS],
                deny=[value for value in (deny or []) if value in FILE_TOOLS], confirm=approve)

    started = monotonic()
    try:
        if workspace.resolve() != workspace:
            raise ValueError("任务工程不能通过链接重定向。")
        workspace.mkdir(parents=True)
        for relative, content in case["files"].items():
            path = _path(workspace, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content.encode("utf-8"))
        with tool_workspace(workspace):
            row["output"] = run_agent_loop(messages, model, max_turns, execute)
        row["status"] = "complete"
    except KeyboardInterrupt:
        row["status"] = "cancelled"
    except Exception as failure:
        error = {"type": type(failure).__name__}
        error.update({"status_code": getattr(failure, "status_code", None)} if isinstance(failure, MODEL_API_ERRORS) else {"message": str(failure)})
        row.update(status="error", error=error)
    finally:
        row.update(messages=messages, trace=_trace(messages), elapsed_seconds=round(monotonic() - started, 4))
        try:
            row["files_after"] = _inventory(workspace)
            row["checks"] = (_checks(case, workspace, row["trace"], row["files_after"]) if artifact_validator is None else
                artifact_validator(case, workspace) + _execution_checks(case, row["trace"], row["files_after"], outputs))
            if row["status"] == "complete":
                row["all_passed"] = all(check["passed"] for check in row["checks"])
                row["score"] = round(sum(check["passed"] for check in row["checks"]) / len(row["checks"]), 4)
        except (ValueError, OSError, RuntimeError) as failure:
            row.update(status="error", inspection_error=type(failure).__name__, score=None, all_passed=False)
    return row


def evaluate_tool_tasks(evaluator, candidate_id, suite_path, call_model, *, model_settings,
                        permission_mode="default", allow=None, deny=None, confirm=None, max_turns=5, prices=None, check_budget=None):
    suite, suite_path, suite_hash = load_tool_suite(evaluator.root, suite_path)
    candidate, feedback, active, versions, baseline_kind = _versions(evaluator, candidate_id)
    if suite["skill_name"] != candidate["skill_name"] or any(_normalized(case["prompt"]) == _normalized(feedback["task"]) for case in suite["cases"]):
        raise ValueError("样本技能名须匹配，任务不能直接重复候选的源任务。")
    if permission_mode not in PERMISSION_MODES or type(max_turns) is not int or max_turns < 1:
        raise ValueError("回放权限模式或 max_turns 无效。")
    initial_state = _active_state(active)
    candidate_path = evaluator.evolution._path("candidate", candidate_id)
    candidate_hash = _digest(candidate_path.read_bytes())
    available = FILE_TOOLS if versions["candidate"].allowed_tools is None else FILE_TOOLS & frozenset(versions["candidate"].allowed_tools)
    report = {"version": 1, "id": "eval_" + uuid4().hex, "project_root": str(evaluator.root),
              "created_at": datetime.now(timezone.utc).isoformat(), "candidate_id": candidate_id, "candidate_status": candidate["status"],
              "skill_name": candidate["skill_name"], "base_sha256": candidate["base_sha256"], "feedback_id": feedback["id"],
              "evaluation_kind": "tool_task", "mode": "file_tools", "baseline_kind": baseline_kind,
              "suite": suite, "suite_path": suite_path, "suite_sha256": suite_hash, "model_settings": model_settings,
              "max_turns_per_variant": max_turns, "permission_mode": permission_mode, "allowed_file_tools": sorted(available),
              "prices": {"input_per_million": str(prices.input_per_million), "output_per_million": str(prices.output_per_million)} if prices else None,
              "allow": allow, "deny": deny, "task_prompt": TASK_PROMPT,
              "methods": {key: {"text": value.prompt_template, "sha256": _digest(value.prompt_template.encode())} for key, value in versions.items()},
              "rows": [], "status": "complete"}
    stopped = False
    for index, case in enumerate(suite["cases"]):
        for variant, method in versions.items():
            if stopped:
                report["rows"].append({"case_id": case["id"], "variant": variant, "status": "not_run", "all_passed": False, "score": None, "calls": [], "trace": [], "checks": []})
                continue
            workspace = evaluator.root / ".personal/skill-evals/workspaces" / report["id"] / f"case-{index + 1:02d}" / variant
            def approve(tool, values, reason):
                print(f"回放工具审批：{case['id']}/{variant}；工程：{workspace}")
                return confirm(tool, values, reason) if confirm is not None else False
            row = run_file_task(case, method, workspace, call_model, variant=variant, available=available,
                permission_mode=permission_mode, allow=allow, deny=deny, confirm=approve, max_turns=max_turns,
                prices=prices, check_budget=check_budget)
            row["workspace"] = workspace.relative_to(evaluator.root).as_posix()
            report["rows"].append(row)
            if row["status"] in {"cancelled", "error"}:
                report["status"], stopped = ("cancelled" if row["status"] == "cancelled" else "partial"), True
    report["summary"] = {}
    for variant in versions:
        rows = [row for row in report["rows"] if row["variant"] == variant]
        report["summary"][variant] = {"completed": sum(row["status"] == "complete" for row in rows), "total": len(rows),
                                       "passed_tasks": sum(row["all_passed"] for row in rows),
                                       "mean_score": round(sum(row["score"] for row in rows) / len(rows), 4) if all(row["score"] is not None for row in rows) else None}
    calls = [entry for row in report["rows"] for entry in row["calls"]]
    known = [entry["usage"] for entry in calls if entry["usage"] is not None]
    started = sum(entry["logical_call_started"] for entry in calls)
    report["usage_summary"] = {"logical_calls_started": started, "complete_usage_reports": len(known),
                               "reported_tokens": {key: sum(usage[key] for usage in known) for key in ("prompt_tokens", "completion_tokens", "total_tokens")} if known else None,
                               "estimated_cost_yuan": str(sum(Decimal(entry["estimated_cost_yuan"]) for entry in calls if entry["estimated_cost_yuan"] is not None)) if prices and started and len(known) == started else None}
    report["active_skill_unchanged"] = _active_state(active) == initial_state
    try:
        report["candidate_unchanged"] = _digest(candidate_path.read_bytes()) == candidate_hash
    except OSError:
        report["candidate_unchanged"] = False
    if report["status"] != "complete":
        report["decision"] = "incomplete"
    elif not report["active_skill_unchanged"] or not report["candidate_unchanged"]:
        report["decision"] = "not_comparable"
    else:
        base, proposed = (report["summary"][key]["passed_tasks"] for key in ("baseline", "candidate"))
        report["decision"] = "candidate_better_on_tasks" if proposed > base else "baseline_better_on_tasks" if base > proposed else "tie_on_tasks"
    report["boundary"] = "固定文件工具、独立工程和实际产物检查；holdout 为用户声明。add 与无技能对照比较，merge 与原方法比较；不执行完整 fork/资源包或 Shell/MCP，不自动采纳或晋升 champion，不代表泛化或语义质量提升。"
    evaluator._save_new(evaluator._path(report["id"]), report)
    return report


def format_tool_evaluation(report, *, detail=False):
    lines = [f"工具任务评估：{report['id']} [{report['status']}]；候选：{report['candidate_id']}",
             f"基线：{report['baseline_kind']}；样本：{report['suite']['name']} [{report['suite']['split']}]；结论：{report['decision']}"]
    for variant, summary in report["summary"].items():
        lines.append(f"- {variant}：完成 {summary['completed']}/{summary['total']}；任务通过 {summary['passed_tasks']}/{summary['total']}；检查均分 {summary['mean_score']}")
    for row in report["rows"]:
        lines.append(f"  {row['case_id']}/{row['variant']} [{row['status']}]；工程：{row.get('workspace', '未运行')}；工具结果 {len(row['trace'])} 条")
        if "error" in row:
            lines.append("  请求/循环错误：" + json.dumps(row["error"], ensure_ascii=False))
        if "inspection_error" in row:
            lines.append("  产物检查错误：" + row["inspection_error"])
        for check in row["checks"]:
            if not check["passed"]:
                lines.append("  检查失败：" + json.dumps(check, ensure_ascii=False))
        if detail:
            lines.append("  实际工具轨迹：" + json.dumps(row["trace"], ensure_ascii=False))
            lines.append("  最终输出/错误：" + str(row.get("output", row.get("error", "未完成"))))
    lines.append("用量：" + json.dumps(report["usage_summary"], ensure_ascii=False))
    lines.append(report["boundary"])
    return "\n".join(lines)
