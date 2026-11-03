"""有界评估套件与不可变输入副本；独立回归测试不进入模型工程。"""

import json
import re

from .configured_tests import copy_snapshot, load_test_config
from .file_runtime import plain_path, write_json_atomic
from .skill_task_eval import _fixture_name
from .source_snapshot import digest, snapshot_sources
from .structured_output import strict_json
from .rule_evidence import load_rules
from .junit_import import load_junit_report


def relative_path(root, value):
    _fixture_name("playground/" + value if isinstance(value, str) else value)
    path = plain_path(root / value)
    if not path.is_relative_to(root):
        raise ValueError("评估套件路径越界。")
    return path


def _project_fingerprint(snapshot):
    return digest(json.dumps(snapshot["manifest"], sort_keys=True).encode("utf-8"))


def load_suite(path):
    path = plain_path(path)
    with path.open("rb") as source:
        raw = source.read(65537)
    if len(raw) > 65536:
        raise ValueError("评估清单超过 64 KiB。")
    value = strict_json(raw.decode("utf-8-sig"))
    if (not isinstance(value, dict) or set(value) != {"version", "name", "cases"} or type(value["version"]) is not int
            or value["version"] != 1 or not isinstance(value["name"], str) or not 1 <= len(value["name"]) <= 100
            or not isinstance(value["cases"], list) or not 1 <= len(value["cases"]) <= 10):
        raise ValueError("评估清单需有 version=1、name 和 1～10 个 cases。")
    cases, ids, fingerprints = [], set(), set()
    for item in value["cases"]:
        required = {"id", "split", "project", "test_config", "regression_tests"}
        if (not isinstance(item, dict) or not required <= set(item) or set(item) - required - {"rules", "junit_report"}
                or not isinstance(item["id"], str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", item["id"])
                or item["split"] not in {"development", "holdout"} or item["id"] in ids):
            raise ValueError("评估样本字段、唯一 ID 或 development/holdout 划分无效。")
        project = snapshot_sources(relative_path(path.parent, item["project"]))
        regression = snapshot_sources(relative_path(path.parent, item["regression_tests"])) if item["regression_tests"] is not None else None
        if (any(name == "evaluation_tests" or name.startswith("evaluation_tests/") for name in project["files"])
                or regression is not None and any(not name.endswith(".py") for name in regression["files"])):
            raise ValueError("项目不能占用 evaluation_tests；独立回归输入仅支持 .py。")
        if regression is not None and (project["root"].is_relative_to(regression["root"]) or regression["root"].is_relative_to(project["root"])):
            raise ValueError("公开项目与独立回归测试不能嵌套。")
        config_raw, settings, config_source = load_test_config(relative_path(path.parent, item["test_config"]), project["manifest"])
        rules = load_rules(relative_path(path.parent, item["rules"]), project) if "rules" in item else None
        if rules is not None and regression is not None and plain_path(rules["source"]["path"]).is_relative_to(regression["root"]):
            raise ValueError("规则文件不能位于独立回归目录内。")
        fingerprint = _project_fingerprint(project)
        if fingerprint in fingerprints:
            raise ValueError("套件包含精确重复的公开项目，不能作为不同开发/保留样本。")
        ids.add(item["id"]); fingerprints.add(fingerprint)
        supplied = load_junit_report(relative_path(path.parent, item["junit_report"])) if "junit_report" in item else None
        cases.append({**item, "project_snapshot": project, "regression_snapshot": regression,
            "config_raw": config_raw, "settings": settings, "config_source": config_source, "rules_input": rules, "supplied_junit": supplied})
    return {"path": path, "raw": raw, "sha256": digest(raw), "name": value["name"], "cases": cases}


def freeze_suite(suite, directory, split, parents=None):
    selected = [case for case in suite["cases"] if split == "all" or case["split"] == split]
    if not selected:
        raise ValueError("所选分组没有样本。")
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "suite-manifest.json").write_bytes(suite["raw"])
    entries = []
    for case in selected:
        base = directory / "cases" / case["id"]
        copy_snapshot(case["project_snapshot"], base / "project")
        if case["regression_snapshot"] is not None:
            copy_snapshot(case["regression_snapshot"], base / "regression")
        (base / "test-config.json").write_bytes(case["config_raw"])
        entries.append({"id": case["id"], "split": case["split"], "project_manifest": case["project_snapshot"]["manifest"],
            "regression_manifest": case["regression_snapshot"]["manifest"] if case["regression_snapshot"] else None, "config_sha256": digest(case["config_raw"]),
            "origin_project": str(case["project_snapshot"]["root"]), "origin_regression": str(case["regression_snapshot"]["root"]) if case["regression_snapshot"] else None,
            "origin_config": case["config_source"]})
        if case["rules_input"] is not None:
            (base / "rules.json").write_bytes(case["rules_input"]["raw"])
            entries[-1]["origin_rules"] = case["rules_input"]["source"]
        if parents is not None:
            entries[-1]["origin_parent"] = parents["references"][case["id"]]
        if case["supplied_junit"] is not None:
            (base / "provided.xml").write_bytes(case["supplied_junit"][0])
            entries[-1]["origin_junit"] = case["supplied_junit"][1]
    lock = {"version": 1, "suite_source": str(suite["path"]), "suite_sha256": suite["sha256"], "cases": entries}
    if parents is not None:
        (directory / "parents.json").write_bytes(parents["raw"])
        lock["parent_map_source"] = parents["source"]
    write_json_atomic(directory / "inputs.lock.json", lock)
    from .repair_provenance import mark_evaluation_projects
    mark_evaluation_projects(directory, lock)
    return lock


def check_inputs(directory, lock):
    suite = load_suite(lock["suite_source"])
    if suite["sha256"] != lock["suite_sha256"] or digest((directory / "suite-manifest.json").read_bytes()) != lock["suite_sha256"]:
        raise ValueError("评估套件清单版本变化。")
    current = {case["id"]: case for case in suite["cases"]}
    if "parent_map_source" in lock:
        info = lock["parent_map_source"]
        raw = plain_path(info["path"]).read_bytes()
        if digest(raw) != info["sha256"] or len(raw) != info["bytes"] or (directory / "parents.json").read_bytes() != raw:
            raise ValueError("父诊断映射原输入或冻结副本变化。")
    for item in lock["cases"]:
        case = current[item["id"]]
        base = directory / "cases" / item["id"]
        for label, snapshot, expected in (("project", case["project_snapshot"], item["project_manifest"]),
                ("regression", case["regression_snapshot"], item["regression_manifest"])):
            if snapshot is None:
                if expected is not None or (base / label).exists():
                    raise ValueError("没有提供的独立测试不能被补写或伪造。")
                continue
            if snapshot["manifest"] != expected or snapshot_sources(base / label)["manifest"] != expected:
                raise ValueError("评估原输入或冻结副本变化。")
        if case["config_source"] != item["origin_config"] or digest((base / "test-config.json").read_bytes()) != item["config_sha256"]:
            raise ValueError("评估配置版本变化。")
        if case["rules_input"] is not None:
            if case["rules_input"]["source"] != item.get("origin_rules") or (base / "rules.json").read_bytes() != case["rules_input"]["raw"]:
                raise ValueError("评估规则原输入或冻结字节变化。")
        elif "origin_rules" in item:
            raise ValueError("评估的规则绑定被移除。")
        if case["supplied_junit"] is not None:
            if case["supplied_junit"][1] != item.get("origin_junit") or (base / "provided.xml").read_bytes() != case["supplied_junit"][0]:
                raise ValueError("提供的 JUnit 原文件或冻结副本变化。")
        elif "origin_junit" in item:
            raise ValueError("JUnit 来源绑定被移除。")
        if "origin_parent" in item:
            from .diagnosis_followup import prepare_parent
            expected = prepare_parent(item["origin_parent"]["path"], case["project_snapshot"]["manifest"], case["rules_input"])
            if expected != item["origin_parent"]:
                raise ValueError("原父诊断与输入锁绑定不一致。")


def check_case_rules(base, case, diagnosis, note):
    if "origin_rules" in case:
        expected = {**case["origin_rules"], "path": str(base / "rules.json"), "review_note": note}
        if diagnosis.get("rules_source") != expected:
            raise ValueError("案例诊断的规则来源或审阅说明与原输入锁不一致。")
    elif "rules_source" in diagnosis:
        raise ValueError("案例诊断注入了输入锁之外的规则。")
    if diagnosis.get("parent_diagnosis") != case.get("origin_parent"):
        raise ValueError("案例父诊断引用与输入锁不一致。")
