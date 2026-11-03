"""实际 pytest 失败/修复两轮 → JUnit → JSONL → CI；默认固定模型，--live 才请求模型。"""

import argparse
from importlib.metadata import version
import json
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex.verify_ci_tasks import fixed_model
from codex.verify_delivery import isolated
from personal_agent.ci_runner import METHOD_PATH, run_ci_logs
from personal_agent.junit_import import convert_junit_report, parse_junit, source_matches
from personal_agent.pytest_demo import DEMO, code_hashes, execute_pytest


FIXES = {
    "checkout.py": ("return round(subtotal * (1 - discount_percent), 2)",
                    "return round(subtotal * (1 - discount_percent / 100), 2)"),
    "demo_cases.py": ('raise RuntimeError("demonstration gateway configuration is missing")',
                      'return {"mode": "sandbox"}'),
}


def prepare_method(root):
    method = root / METHOD_PATH
    method.parent.mkdir(parents=True, exist_ok=True)
    method.write_bytes((ROOT / METHOD_PATH).read_bytes())


def run_pytest(directory, xml, *, fixed=False):
    shutil.copytree(DEMO, directory)
    before = code_hashes(directory)
    changes = []
    if fixed:
        for name, (old, new) in FIXES.items():
            file = directory / name
            text = file.read_text(encoding="utf-8")
            assert text.count(old) == 1
            file.write_text(text.replace(old, new), encoding="utf-8", newline="\n")
            changes.append({"file": name, "before": old, "after": new})
    result = execute_pytest(directory, xml)
    assert result["returncode"] == (0 if fixed else 1), f"unexpected pytest exit; see {result['log']}"
    result.update(scripted_changes=changes, source_before=before)
    return result


def flow(root, *, live=False):
    prepare_method(root)
    original = code_hashes(DEMO)
    work = root / "evidence"
    broken = run_pytest(work / "broken-project", work / "xml/broken.xml")
    fixed = run_pytest(work / "fixed-project", work / "xml/fixed.xml", fixed=True)
    with isolated(root), patch("personal_agent.task_runner.ROOT", root):
        imports = [convert_junit_report(item["xml"], output_dir=work / "normalized" / label, run_id="demo-" + label)
            for label, item in (("broken", broken), ("fixed", fixed))]
        assert imports[0]["summary"] == {"total": 5, "passed": 2, "failed": 2, "skipped": 1, "failure_nodes": 1, "error_nodes": 1}
        assert imports[1]["summary"] == {"total": 5, "passed": 4, "failed": 0, "skipped": 1, "failure_nodes": 0, "error_nodes": 0}
        for item in imports:
            directory = Path(item["directory"])
            assert (directory / "source.xml").read_bytes() == Path(item["source"]["path"]).read_bytes()
            assert "summary" not in json.loads((directory / "run.json").read_text(encoding="utf-8"))
            mapping = json.loads((directory / "junit-map.json").read_text(encoding="utf-8"))["entries"]
            records = [json.loads(line) for line in (directory / "results.jsonl").read_text(encoding="utf-8").splitlines()]
            assert [row["line"] for row in mapping] == list(range(1, 6))
            assert [row["test"] for row in mapping] == [row["test"] for row in records]
        triage = run_ci_logs(batch_dir=work / "normalized", output_dir=work / "triage", call_model=None if live else fixed_model())
        assert triage["status"] == "validated" and triage["summary"]["validated"] == 2
        assert [row["ci_observation"] for row in triage["rows"]] == ["failures_reported", "no_failures_in_log"]
        assert all(source_matches(item["source"]) for item in imports) and code_hashes(DEMO) == original
        return {"status": "passed", "validation": "actual pytest/XML/file tools with real model" if live else "actual pytest/XML/file tools with fixed model/usage",
            "project": str(root), "pytest_runs": {"broken": broken, "fixed": fixed}, "conversions": imports,
            "triage": triage, "usage": triage["usage"], "source_xmls_and_demo_unchanged": True,
            "mapping_checked": True, "boundary": "self-authored executable fixture; two explicit scripted fixes, not Agent repair or external CI; skipped test remains"}


def verify_workflow():
    with tempfile.TemporaryDirectory(prefix="pytest-junit-链路 ") as temporary:
        result = flow(Path(temporary).resolve())
        return {"status": "passed", "pytest_version": version("pytest"), "actual_pytest_exits": [1, 0],
            "testcases_per_run": 5, "scripted_fixes": 2, "mapping_and_raw_xml_verified": True,
            "fixed_model_calls": result["usage"]["calls"], "demo_source_unchanged": True}


def reject(action):
    try:
        action()
    except ValueError:
        return
    raise AssertionError("unsupported XML accepted")


def verify_guards():
    with tempfile.TemporaryDirectory(prefix="junit-import-guards-") as temporary:
        root = Path(temporary).resolve()
        prepare_method(root)
        # 人工 XML 仅用于格式/错误分支；实际 pytest 输出在 workflow 中单独验证。
        xml = ('<?xml version="1.0" encoding="utf-16"?><testsuites xmlns="urn:junit"><testsuite name="外层">'
            '<testsuite name="inner"><testcase classname="Case" name="same"><failure message="failure detail"/></testcase>'
            '<testcase classname="Case" name="skip"><skipped message="not available"/></testcase></testsuite>'
            '<testcase classname="Other" name="same"/></testsuite></testsuites>').encode("utf-16")
        path = root / "输入 报告.xml"
        path.write_bytes(xml)
        with isolated(root), patch("personal_agent.task_runner.ROOT", root):
            direct = run_ci_logs(junit_report=path, junit_run_id="namespace-demo", call_model=fixed_model())
            assert direct["status"] == "validated" and direct["junit_source_unchanged"]
            assert direct["junit_import"]["summary"]["total"] == 3
            model = fixed_model()
            def changed(*args, **kwargs):
                path.write_bytes(xml + " ".encode("utf-16-le"))
                return model(*args, **kwargs)
            source_changed = run_ci_logs(junit_report=path, call_model=changed)
            assert source_changed["status"] == "failed" and source_changed["rows"][0]["contract_validated"]
            assert source_changed["inputs_unchanged"] and not source_changed["junit_source_unchanged"]
            path.write_bytes(xml)
            saved = Path(direct["directory"]) / "report.json"
            before = saved.read_bytes()
            no_calls = fixed_model()
            reject(lambda: run_ci_logs(junit_report=path, output_dir=direct["directory"], call_model=no_calls))
            assert not no_calls.calls and saved.read_bytes() == before
            empty = root / "empty.xml"
            empty.write_text('<testsuite name="empty" tests="0"/>', encoding="utf-8")
            empty_report = run_ci_logs(junit_report=empty, call_model=fixed_model())
            assert empty_report["status"] == "validated" and empty_report["rows"][0]["ci_observation"] == "no_valid_tests"
            for value in (
                b'<testsuite>', b'<other/>', b'<testsuite><testcase/></testsuite>',
                b'<testsuite><testcase name="x"><rerunFailure/></testcase></testsuite>',
                b'<testsuite><testcase name="x"/><testcase name="x"/></testsuite>',
                b'<testsuite><testcase name="x"><failure/><error/></testcase></testsuite>',
            ):
                reject(lambda value=value: parse_junit(value))
            for encoding in ("utf-8", "utf-16"):
                dtd = ('<?xml version="1.0" encoding="' + encoding + '"?><!DOCTYPE testsuite [<!ENTITY x "expanded">]>'
                    '<testsuite><testcase name="&x;"/></testsuite>').encode(encoding)
                reject(lambda: parse_junit(dtd))
            with patch("personal_agent.junit_import.MAX_XML_BYTES", 4):
                reject(lambda: convert_junit_report(path))
            reject(lambda: convert_junit_report(path, run_id=""))
            return {"status": "passed", "namespaces_nested_suites_utf16_and_skips_verified": True,
                "direct_xml_ci_and_raw_source_change_verified": True, "empty_suite_not_green": True,
                "malformed_unknown_outcome_duplicate_multiple_outcome_and_dtd_rejected": True,
                "size_run_id_and_existing_output_guard_verified": True}


def entrypoint():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--keep-demo", action="store_true", help="固定模型也保留可查看的 pytest/XML/分诊演示工程")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or ROOT / "codex/acceptance-runs" / f"junit-ci-{uuid4().hex}.json"
    if output.exists():
        parser.error("报告已存在，未执行或覆盖。")
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"workflow": verify_workflow(), "guards": verify_guards()}
    if args.live or args.keep_demo:
        project = ROOT / ".personal/junit-demos" / uuid4().hex
        project.mkdir(parents=True, exist_ok=False)
        try:
            report["live" if args.live else "demo"] = flow(project, live=args.live)
        except Exception as error:
            report["live" if args.live else "demo"] = {"status": "failed", "project": str(project), "error_type": type(error).__name__, "message": str(error)}
    report["status"] = "passed" if all(value["status"] == "passed" for value in report.values()) else "failed"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "output": str(output),
        "project": report.get("live", report.get("demo", {})).get("project")}, ensure_ascii=False))
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    entrypoint()
