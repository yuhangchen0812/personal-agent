"""双协议真实 SDK/固定 HTTP 的结构化诊断、工具读取和错误留档；无外部请求。"""

import argparse
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
from uuid import uuid4

import httpx2
from openai import OpenAI
from anthropic import Anthropic

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.support.verify_configured_tests import NOTE, prepare, fixed_model
from tests.support.verify_delivery import message
from tests.support.verify_dual_protocol import response as anthropic_response
from personal_agent.ci_diagnosis import load_checked_diagnosis, recover_saved_diagnosis, run_ci_diagnosis
from personal_agent.ci_repair import apply_repair, rollback_repair
from personal_agent.configured_tests import run_project_tests
from personal_agent.configured_verification import verify_configured_proposal
from personal_agent.diagnosis_contract import RUN, FAILURES
from personal_agent.file_runtime import write_json_atomic
from personal_agent.llm import get_model_message
from personal_agent.source_snapshot import PREFIX, snapshot_sources
from personal_agent.usage import TokenPrices


def setup(root):
    source, config = prepare(root)
    baseline = run_project_tests(source, config, NOTE, output_dir=root / "baseline")
    assert baseline["status"] == "tests_failed"
    return source, config, root / "baseline/run.xml"


def sdk_model(protocol="openai", mode="correct"):
    requests, histories = [], []
    config = {"protocol": protocol, "model": "offline-model", "api_key": "offline", "base_url": "https://example.invalid"}
    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        terminal = "response_format" in body or "output_config" in body
        if terminal:
            assert "tools" not in body and "tool_choice" not in body
            if mode == "unsupported":
                return httpx2.Response(400, json={"error": {"type": "invalid_request_error", "message": "unsupported schema"}})
            final = fixed_model(histories[-1], [], on_usage=lambda _: None)
            if mode == "format":
                final["content"] = "说明\n```json\n" + final["content"] + "\n```"
            elif mode == "fence":
                final["content"] = "```json\n" + final["content"] + "\n```"
            elif mode == "duplicate":
                final["content"] = '{"summary":"a",' + final["content"][1:]
            elif mode == "schema":
                final["content"] = '{"summary":"missing fields"}'
            elif mode == "evidence":
                value = json.loads(final["content"])
                value["edits"][0]["evidence"][0]["start_line"] = 999
                value["edits"][0]["evidence"][0]["end_line"] = 999
                final["content"] = json.dumps(value)
            elif mode == "escaped_id":
                value = json.loads(final["content"])
                value["failures"][0]["test"] = json.dumps(value["failures"][0]["test"])
                final["content"] = json.dumps(value)
            if mode == "truncated":
                final["content"] = '{"summary":'
            if mode == "refusal":
                final.update(content=None, refusal="offline refusal")
        elif len(requests) == 1:
            assert {item["function"]["name"] if protocol == "openai" else item["name"] for item in body["tools"]} == {"read_file", "list_files", "grep_search"}
            final = message(("run", "read_file", {"file_path": RUN}), ("failures", "read_file", {"file_path": FAILURES}),
                ("code", "read_file", {"file_path": PREFIX + "billing/fees.py"}), ("tests", "read_file", {"file_path": PREFIX + "tests/test_fees.py"}))
        else:
            final = {"role": "assistant", "content": "已读取失败行和实现/测试；阈值等号疑似遗漏，尚未测试。"}
        if protocol == "openai":
            reason = "length" if terminal and mode == "truncated" else "tool_calls" if final.get("tool_calls") else "stop"
            return httpx2.Response(200, json={"id": "offline", "object": "chat.completion", "created": 0, "model": config["model"],
                "choices": [{"index": 0, "finish_reason": reason, "message": final}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}})
        if final.get("tool_calls"):
            blocks = [{"type": "tool_use", "id": item["id"], "name": item["function"]["name"], "input": json.loads(item["function"]["arguments"])} for item in final["tool_calls"]]
            reason = "tool_use"
        else:
            blocks = [{"type": "text", "text": final["content"] or "offline refusal"}]
            reason = "max_tokens" if terminal and mode == "truncated" else "refusal" if terminal and mode == "refusal" else "end_turn"
        return httpx2.Response(200, json=anthropic_response(blocks, reason))
    def model(history, definitions, **options):
        histories.append(deepcopy(history))
        if mode == "cancel" and options.get("response_schema") is not None:
            raise KeyboardInterrupt()
        factory = OpenAI if protocol == "openai" else Anthropic
        target = "personal_agent.llm.OpenAI" if protocol == "openai" else "personal_agent.anthropic_adapter.Anthropic"
        def client(**settings):
            return factory(**settings, http_client=httpx2.Client(transport=httpx2.MockTransport(handle)))
        with patch("personal_agent.llm.load_model_config", return_value=config), patch(target, side_effect=client):
            return get_model_message(history, definitions, **options)
    model.requests, model.histories = requests, histories
    return model


def verify_workflow():
    with tempfile.TemporaryDirectory(prefix="structured-diagnosis-") as temporary:
        root = Path(temporary).resolve()
        source, config, xml = setup(root)
        before = snapshot_sources(source)["files"]
        schemas = []
        for protocol in ("openai", "anthropic"):
            model = sdk_model(protocol)
            result = run_ci_diagnosis(xml, source, call_model=model, output_dir=root / protocol, max_turns=3)
            assert result["status"] == "evidence_checked", result.get("failure")
            assert result["output_mode"] == "json_schema" and result["usage"]["calls"] == result["usage"]["reported_calls"] == 3
            load_checked_diagnosis(root / protocol / "report.json")
            options = model.requests[-1]["response_format"]["json_schema"] if protocol == "openai" else model.requests[-1]["output_config"]["format"]
            schemas.append(options["schema"])
            assert all("response_format" not in item and "output_config" not in item for item in model.requests[:-1])
            assert result["requests"][-1]["tools"] == []
            # Schema 动态枚举包含原始标识，JSON 解码后无需再转义一次。
            identifier = result["diagnosis"]["failures"][0]["test"]
            assert options["schema"]["properties"]["failures"]["items"]["properties"]["test"]["enum"] == [identifier]
        assert schemas[0] == schemas[1]
        receipt = verify_configured_proposal(root / "openai/report.json", config, NOTE, output_dir=root / "verification")
        assert receipt["status"] == "verified_with_test_config" and receipt["summary"]["passed"] == 4
        assert apply_repair(root / "verification/receipt.json", NOTE, output_dir=root / "repair")["status"] == "applied"
        assert rollback_repair(root / "repair", NOTE)["status"] == "rolled_back"
        assert snapshot_sources(source)["files"] == before
        report = root / "openai/report.json"
        raw = report.read_bytes()
        value = json.loads(raw); value["requests"][-1]["response_schema"] = {"type": "object"}
        write_json_atomic(report, value)
        try:
            load_checked_diagnosis(report)
        except ValueError:
            pass
        else:
            raise AssertionError("changed final schema accepted")
        report.write_bytes(raw)
        return {"status": "passed", "real_sdk_fixed_http_protocols": ["openai", "anthropic"], "logical_calls_per_protocol": 3,
            "tool_investigation_then_schema_without_tools": True, "native_wire_parameters_and_ids_verified": True,
            "actual_candidate_pytest_passed": 4, "adoption_and_byte_rollback": True, "schema_version_rechecked": True, "new_network_calls": 0}


def verify_guards():
    with tempfile.TemporaryDirectory(prefix="structured-guards-") as temporary:
        root = Path(temporary).resolve()
        source, _, xml = setup(root)
        before = snapshot_sources(source)["files"]
        kinds = {"format": "output_format", "fence": "output_format", "duplicate": "output_format", "schema": "schema", "evidence": "evidence",
            "escaped_id": "evidence", "refusal": "refusal", "truncated": "output_truncated", "unsupported": "request_failed", "cancel": "cancelled"}
        for mode, kind in kinds.items():
            model = sdk_model(mode=mode)
            result = run_ci_diagnosis(xml, source, call_model=model, output_dir=root / mode, max_turns=3)
            assert result["failure"]["kind"] == kind, (mode, result)
            assert result["status"] in {"partial", "rejected", "cancelled"}
            assert not (root / mode / "candidate-source").exists() and len(model.histories) == 3
            if mode == "unsupported":
                assert len(model.requests) == 3 and result["failure"]["http_status"] == 400
            if mode in {"format", "duplicate"}:
                assert (root / mode / "model-response.txt").read_text(encoding="utf-8") == result["messages"][-1]["content"]
            if mode == "fence":
                try:
                    recover_saved_diagnosis(root / mode / "report.json")
                except ValueError:
                    pass
                else:
                    raise AssertionError("structured mode downgraded through legacy fence recovery")
            if mode in {"refusal", "truncated"}:
                assert (root / mode / "terminal-error-response.json").is_file() and result["usage"]["reported_calls"] == 3
        model = sdk_model()
        limited = run_ci_diagnosis(xml, source, call_model=model, output_dir=root / "budget", max_turns=3,
            prices=TokenPrices(Decimal(1), Decimal(1)), max_cost=Decimal("0.000006"))
        assert limited["failure"]["kind"] == "budget" and len(model.requests) == limited["usage"]["calls"] == 2
        # Anthropic refusal/truncation 也通过真实 SDK 验证，不声称远端已验证。
        for mode in ("refusal", "truncated"):
            result = run_ci_diagnosis(xml, source, call_model=sdk_model("anthropic", mode), output_dir=root / ("anthropic-" + mode), max_turns=3)
            assert result["failure"]["kind"] == kinds[mode] and result["usage"]["reported_calls"] == 3
        assert snapshot_sources(source)["files"] == before
        return {"status": "passed", "malformed_prose_duplicate_schema_evidence_and_double_encoding_rejected": True,
            "both_protocol_refusal_and_truncation_preserve_usage_and_raw_text": True,
            "unsupported_400_no_downgrade_or_semantic_retry": True, "final_budget_and_cancellation_enforced": True,
            "all_failed_cases_have_no_candidate_and_keep_source_bytes": True, "new_network_calls": 0}
