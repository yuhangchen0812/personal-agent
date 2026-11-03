"""只读导入 pytest/JUnit XML 子集，保存原始快照、JSONL 和逐项来源映射；不执行测试。"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4
import xml.etree.ElementTree as ET

from . import task_runner as runner
from .file_runtime import plain_path, write_json_atomic


MAX_XML_BYTES = 1024 * 1024
MAX_NATIVE_BYTES = 1024 * 1024
MAX_TESTCASES = 10000
OUTCOMES = {"failure", "error", "skipped"}
INFORMATION = {"properties", "system-out", "system-err"}


class _NoDtd(ET.TreeBuilder):
    def doctype(self, *args):
        raise ValueError("JUnit 导入不接受 DTD/实体声明。")


def _tag(element):
    return element.tag.rsplit("}", 1)[-1]


def _read_xml(path):
    path = plain_path(path)
    if not path.is_file():
        raise ValueError("JUnit 报告必须是存在的普通 XML 文件。")
    with path.open("rb") as source:
        raw = source.read(MAX_XML_BYTES + 1)
    if len(raw) > MAX_XML_BYTES:
        raise ValueError("JUnit XML 超过 1 MiB 上限。")
    return raw, {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def source_matches(source):
    try:
        return _read_xml(source["path"])[1] == source
    except (ValueError, OSError):
        return False


def parse_junit(raw):
    """按文档顺序提取 testcase，不信任汇总属性、不推断重试，诊断文本仅作为资料。"""
    if len(raw) > MAX_XML_BYTES:
        raise ValueError("JUnit XML 超过 1 MiB 上限。")
    try:
        root = ET.fromstring(raw, parser=ET.XMLParser(target=_NoDtd()))
    except ET.ParseError as error:
        raise ValueError(f"JUnit XML 无法解析：{error}") from None
    if _tag(root) not in {"testsuite", "testsuites"}:
        raise ValueError("只支持 testsuite/testsuites 根节点的 JUnit XML。")
    records, mapping, identities = [], [], set()

    def testcase(node, suites):
        name, classname = node.get("name", ""), node.get("classname", "")
        if not name.strip() or node.get("status", "run") != "run" or "result" in node.attrib:
            raise ValueError("testcase 需有非空 name；未识别的 status/result 不推断为通过。")
        if len(records) >= MAX_TESTCASES:
            raise ValueError("JUnit testcase 数量超过上限。")
        identity = json.dumps([suites, classname, name], ensure_ascii=False, separators=(",", ":"))
        if identity in identities:
            raise ValueError("出现重复的套件/类/用例标识，不能默认当作重试。")
        identities.add(identity)
        outcomes = [child for child in node if _tag(child) in OUTCOMES]
        if len(outcomes) > 1 or any(_tag(child) not in OUTCOMES | INFORMATION for child in node):
            raise ValueError("每个 testcase 只支持一个 failure/error/skipped；不支持重试扩展或未知结果节点。")
        outcome = _tag(outcomes[0]) if outcomes else "passed"
        diagnostic = None
        if outcomes:
            child = outcomes[0]
            diagnostic = {"type": child.get("type", ""), "message": child.get("message", ""), "text": "".join(child.itertext()).strip()}
            detail = diagnostic["message"] or diagnostic["text"] or diagnostic["type"]
            message = outcome + (": " + detail if detail else "")
        else:
            message = ""
        status = "failed" if outcome in {"failure", "error"} else "skipped" if outcome == "skipped" else "passed"
        records.append({"test": identity, "status": status, "message": message})
        mapping.append({"line": len(records), "test": identity, "suite": suites, "classname": classname, "name": name,
            "outcome": outcome, "attributes": dict(node.attrib), "diagnostic": diagnostic})

    def visit(node, parents, depth=0):
        if depth >= 32:
            raise ValueError("JUnit 套件嵌套超过支持深度。")
        tag = _tag(node)
        suites = parents + [node.get("name") or "(unnamed)"] if tag == "testsuite" else parents
        for child in node:
            child_tag = _tag(child)
            if child_tag in {"testsuite", "testsuites"}:
                visit(child, suites, depth + 1)
            elif child_tag == "testcase" and tag == "testsuite":
                testcase(child, suites)
            elif child_tag not in INFORMATION:
                raise ValueError("JUnit 套件含未支持节点，未静默忽略。")
    visit(root, [])
    counts = Counter(record["status"] for record in records)
    return {"records": records, "mapping": mapping, "summary": {"total": len(records),
        **{state: counts[state] for state in ("passed", "failed", "skipped")},
        "failure_nodes": sum(item["outcome"] == "failure" for item in mapping),
        "error_nodes": sum(item["outcome"] == "error" for item in mapping)}}


def load_junit_report(xml_path):
    raw, source = _read_xml(xml_path)
    parsed = parse_junit(raw)
    return raw, source, parsed


def convert_junit_report(xml_path, *, output_dir=None, run_id=None):
    raw, source, parsed = load_junit_report(xml_path)
    run_id = Path(source["path"]).stem if run_id is None else run_id
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id 必须是非空字符串。")
    identity = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8]
    directory = plain_path(output_dir if output_dir is not None else runner.ROOT / ".personal/junit-imports" / identity)
    if directory.exists():
        raise ValueError("JUnit 输出目录已存在，未覆盖。")
    # 原 XML 和映射留在转换目录；模型只会读 native 输入，不提供本地计算的汇总答案。
    metadata = {"run_id": run_id, "source": {"format": "junit_xml", **source}}
    meta_bytes = (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    # 转义 Unicode 行分隔符等字符，保证每个 testcase 恰占一个 JSONL 物理行；解析后文字不变。
    log_bytes = "".join(json.dumps(record, ensure_ascii=True) + "\n" for record in parsed["records"]).encode("utf-8")
    if max(len(meta_bytes), len(log_bytes)) > MAX_NATIVE_BYTES:
        raise ValueError("归一化后的输入超过 CI 文件大小上限，未创建输出。")
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "source.xml").write_bytes(raw)
    (directory / "run.json").write_bytes(meta_bytes)
    (directory / "results.jsonl").write_bytes(log_bytes)
    write_json_atomic(directory / "junit-map.json", {"version": 1, "source": source, "summary": parsed["summary"], "entries": parsed["mapping"]})
    report = {"version": 1, "kind": "junit_import", "id": identity, "directory": str(directory), "run_id": run_id,
        "source": source, "summary": parsed["summary"], "source_unchanged": source_matches(source),
        "files": {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in
            ("source.xml", "run.json", "results.jsonl", "junit-map.json")},
        "boundary": "JUnit subset; failure/error map to failed, skipped to skipped; no execution, retry inference, root cause or quality claim"}
    report["status"] = "converted" if report["source_unchanged"] else "source_changed"
    write_json_atomic(directory / "conversion.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", dest="xml_path", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--run-id")
    args = parser.parse_args(argv)
    try:
        report = convert_junit_report(**vars(args))
    except (ValueError, OSError) as error:
        print(f"JUnit 导入失败：{error}", file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps({key: report[key] for key in ("status", "directory", "run_id", "summary", "source_unchanged")}, ensure_ascii=False))
    raise SystemExit(0 if report["status"] == "converted" else 1)
