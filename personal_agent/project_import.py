"""直接导入本地 Python 项目和可选 JUnit，生成有来源绑定的任务输入。"""

import json
from pathlib import Path
import re

from .configured_tests import copy_snapshot, load_test_config, parse_test_config
from .file_runtime import plain_path, write_json_atomic
from .junit_import import load_junit_report, source_matches
from .repair_verification import review_text
from .rule_evidence import load_rules, review_rules
from .source_snapshot import digest, snapshot_sources
from .structured_output import strict_json


def create_project_task(source_dir, review_note, *, output_dir, test_config=None, test_paths=None, regression_tests=None, junit_report=None,
                        rules_file=None, rules_review_note=None, parent_diagnosis=None, case_id="project", max_turns=8):
    note = review_text(review_note)
    if not isinstance(case_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", case_id) or type(max_turns) is not int or not 2 <= max_turns <= 50:
        raise ValueError("项目案例 ID 或诊断轮次无效。")
    if test_config is not None and test_paths is not None:
        raise ValueError("--test-config 与 --test-path 不能同时提供。")
    if (rules_file is None) != (rules_review_note is None):
        raise ValueError("规则文件和审阅说明须成对提供。")
    rule_note = review_rules(rules_review_note) if rules_file is not None else None
    project = snapshot_sources(source_dir)
    if test_config is not None:
        config_raw, settings, config_source = load_test_config(test_config, project["manifest"])
    else:
        config_raw = (json.dumps({"version": 1, "runner": "pytest", "test_paths": test_paths or ["tests"], "timeout_seconds": 15, "max_output_bytes": 65536}) + "\n").encode("utf-8")
        settings, config_source = parse_test_config(config_raw, project["manifest"]), None
    regression = snapshot_sources(regression_tests) if regression_tests is not None else None
    if regression is not None and (regression["root"].is_relative_to(project["root"]) or project["root"].is_relative_to(regression["root"]) or any(not name.endswith(".py") for name in regression["files"])):
        raise ValueError("独立测试须为项目之外单独提供的 Python 目录，不能包含其他文本类型。")
    rules = load_rules(rules_file, project) if rules_file is not None else None
    if rules is not None and regression is not None and Path(rules["source"]["path"]).is_relative_to(regression["root"]):
        raise ValueError("规则文件不能位于独立回归目录中。")
    supplied = load_junit_report(junit_report) if junit_report is not None else None
    if supplied is not None and not any(item["outcome"] in {"failure", "error"} for item in supplied[2]["mapping"]):
        raise ValueError("此入口用于失败修复，提供的 JUnit 没有失败。")
    parent = None
    if parent_diagnosis is not None:
        from .diagnosis_followup import prepare_parent
        parent = prepare_parent(parent_diagnosis, project["manifest"], rules, supplied[2] if supplied else None)
    directory = plain_path(output_dir)
    protected = [project["root"], *([regression["root"]] if regression else []), *([Path(parent["path"]).parent] if parent else [])]
    if directory.exists() or any(directory.is_relative_to(path) or path.is_relative_to(directory) for path in protected):
        raise ValueError("项目任务须使用不覆盖原项目/独立测试/父诊断的新目录。")
    imported = directory / "project-source"
    imported.mkdir(parents=True, exist_ok=False)
    copy_snapshot(project, imported / "project")
    if regression:
        copy_snapshot(regression, imported / "regression")
    (imported / "test-config.json").write_bytes(config_raw)
    if rules:
        (imported / "rules.json").write_bytes(rules["raw"])
    if supplied:
        (imported / "provided.xml").write_bytes(supplied[0])
    entry = {"id": case_id, "split": "development", "project": "project", "test_config": "test-config.json", "regression_tests": "regression" if regression else None}
    if rules:
        entry["rules"] = "rules.json"
    if supplied:
        entry["junit_report"] = "provided.xml"
    write_json_atomic(imported / "suite.json", {"version": 1, "name": "direct-project-import", "cases": [entry]})
    origin = {"version": 1, "kind": "ci_project_import", "directory": str(imported), "source_dir": str(project["root"]), "source_manifest": project["manifest"],
        "test_config_source": config_source, "test_config_sha256": digest(config_raw), "settings": settings,
        "regression_source": {"path": str(regression["root"]), "manifest": regression["manifest"]} if regression else None,
        "rules_source": rules["source"] if rules else None, "junit_source": supplied[1] if supplied else None,
        "independent_status": "provided" if regression else "not_provided"}
    write_json_atomic(imported / "origin.json", origin)
    parents_path = None
    if parent:
        parents_path = imported / "parent-links.json"
        write_json_atomic(parents_path, {"version": 1, "parents": [{"case_id": case_id, "diagnosis": parent["path"]}]})
    from .repair_workflow import create_task
    return create_task(imported / "suite.json", note, output_dir=directory, rules_review_note=rule_note, parents_path=parents_path, max_turns=max_turns,
        _project_import={"path": str(imported / "origin.json"), "sha256": digest((imported / "origin.json").read_bytes())})


def check_project_import(path):
    path = plain_path(path)
    value = strict_json(path.read_text(encoding="utf-8"))
    directory = path.parent
    if value.get("version") != 1 or value.get("kind") != "ci_project_import" or value.get("directory") != str(directory):
        raise ValueError("项目导入来源记录无效。")
    if snapshot_sources(value["source_dir"])["manifest"] != value["source_manifest"] or snapshot_sources(directory / "project")["manifest"] != value["source_manifest"]:
        raise ValueError("外部项目或导入副本变化。")
    raw = (directory / "test-config.json").read_bytes()
    if digest(raw) != value["test_config_sha256"] or parse_test_config(raw, value["source_manifest"]) != value["settings"]:
        raise ValueError("导入测试配置变化。")
    if value["test_config_source"] and load_test_config(value["test_config_source"]["path"], value["source_manifest"])[2] != value["test_config_source"]:
        raise ValueError("外部测试配置变化。")
    if value["regression_source"]:
        info = value["regression_source"]
        if snapshot_sources(info["path"])["manifest"] != info["manifest"] or snapshot_sources(directory / "regression")["manifest"] != info["manifest"]:
            raise ValueError("外部独立测试或导入副本变化。")
    elif value["independent_status"] != "not_provided" or (directory / "regression").exists():
        raise ValueError("未提供独立测试时不能伪造其状态。")
    if value["rules_source"]:
        original = Path(value["rules_source"]["path"]).read_bytes()
        if digest(original) != value["rules_source"]["sha256"] or (directory / "rules.json").read_bytes() != original:
            raise ValueError("外部规则或导入副本变化。")
    if value["junit_source"] and (not source_matches(value["junit_source"]) or digest((directory / "provided.xml").read_bytes()) != value["junit_source"]["sha256"]):
        raise ValueError("外部 JUnit 或原字节副本变化。")
    return value
