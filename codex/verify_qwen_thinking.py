"""真实 SDK/固定 HTTP、SSE 的 Qwen 思考工具链；--live 单独做一次有界真实探针。"""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from uuid import uuid4
from unittest.mock import patch

import httpx2
from openai import OpenAI

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex.verify_cli_config import invoke
from codex.verify_delivery import isolated
from personal_agent.agent import run_agent_loop
from personal_agent.anthropic_adapter import prepare_messages
from personal_agent.config import get_model_settings, model_config_scope
from personal_agent.file_runtime import write_json_atomic
from personal_agent.llm import get_model_message, stream_model_message
from personal_agent.model_content import without_provider_state
from personal_agent.qwen_thinking import request_options, validate_state
from personal_agent.session import Session, validate_agent_messages
from personal_agent.source_snapshot import digest
from personal_agent.tool_registry import execute_tool
from personal_agent.usage import UsageStats
from personal_agent.workspace import tool_workspace


MODEL = "qwen3.7-plus"
OPTIONS = {"protocol":"openai", "model":MODEL, "thinking":"enabled", "thinking_budget":512, "max_tokens":2048}
REASON = "synthetic-provider-reasoning\n仅测试字段保真，不代表模型效果。\n"
FINAL_REASON = "synthetic-final-reasoning\n"
USAGE = {"prompt_tokens":10,"completion_tokens":5,"total_tokens":15,"completion_tokens_details":{"reasoning_tokens":2}}
CALLS = [{"id":"read","type":"function","function":{"name":"read_file","arguments":json.dumps({"file_path":"playground/input.txt"})}},
    {"id":"denied","type":"function","function":{"name":"write_file","arguments":json.dumps({"file_path":"playground/denied.txt","content":"NO"})}}]


def response(message, finish="stop"):
    return {"id":"qwen-fixture","object":"chat.completion","created":0,"model":MODEL,
        "choices":[{"index":0,"message":message,"finish_reason":finish}],"usage":USAGE}


def sse(deltas, finish="stop"):
    rows = [{"id":"qwen-fixture","object":"chat.completion.chunk","created":0,"model":MODEL,
        "choices":[{"index":0,"delta":delta,"finish_reason":None}]} for delta in deltas]
    rows += [{"id":"qwen-fixture","object":"chat.completion.chunk","created":0,"model":MODEL,
        "choices":[{"index":0,"delta":{},"finish_reason":finish}]},
        {"id":"qwen-fixture","object":"chat.completion.chunk","created":0,"model":MODEL,"choices":[],"usage":USAGE}]
    content = "".join("data: " + json.dumps(row,ensure_ascii=False) + "\n\n" for row in rows) + "data: [DONE]\n\n"
    return httpx2.Response(200,headers={"content-type":"text/event-stream"},content=content.encode("utf-8"))


def sdk(handler):
    return lambda **options: OpenAI(**options,http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))


def reject(call):
    try:
        call()
    except (ValueError,RuntimeError):
        return
    raise AssertionError("unsupported or invalid reasoning accepted")


def inspect_restore(root):
    with isolated(root):
        with patch("personal_agent.tool_registry.execute_tool",side_effect=AssertionError("no replay")):
            histories=[Session(name).load() for name in ("qwen-json","qwen-stream")]
            assert all(history[1]["qwen"]["reasoning_content"]==REASON for history in histories)
    return {"status":"passed","fresh_process_load_no_tools":True}


def verify_workflow():
    requests=[]
    with tempfile.TemporaryDirectory(prefix="qwen-thinking-中文 空格-") as temporary:
        root=Path(temporary).resolve()
        (root/"playground").mkdir()
        source=root/"playground/input.txt"; source.write_text("ACTUAL_READ_EVIDENCE",encoding="utf-8")
        with isolated(root), patch("personal_agent.config.ENV_FILE",root/"absent.env"), patch.dict(os.environ,
                {"DASHSCOPE_API_KEY":"offline-private","PERSONAL_AGENT_THINKING":"disabled","PERSONAL_AGENT_THINKING_BUDGET":"","PERSONAL_AGENT_MAX_TOKENS":""}):
            def handle(request):
                body=json.loads(request.content); requests.append(body)
                assert body["enable_thinking"] is True and body["thinking_budget"]==512 and body["preserve_thinking"] is True
                assert body["max_completion_tokens"]==2048 and "max_tokens" not in body
                continuing=body["messages"][-1]["role"]=="tool"
                if continuing:
                    assistant=next(item for item in body["messages"] if item["role"]=="assistant")
                    assert assistant["reasoning_content"]==REASON and assistant["tool_calls"]==CALLS
                    assert "权限拒绝" in body["messages"][-1]["content"]
                if body["stream"]:
                    deltas=[{"role":"assistant","reasoning_content":(FINAL_REASON if continuing else REASON)[:5]},
                        {"reasoning_content":(FINAL_REASON if continuing else REASON)[5:]}]
                    if continuing:
                        return sse(deltas+[{"content":"READ_"},{"content":"OK"}])
                    for index,call in enumerate(CALLS):
                        args=call["function"]["arguments"]
                        deltas += [{"tool_calls":[{"index":index,"id":call["id"],"type":"function","function":{"name":call["function"]["name"],"arguments":args[:6]}}]},
                            {"tool_calls":[{"index":index,"function":{"arguments":args[6:]}}]}]
                    return sse(deltas,"tool_calls")
                message={"role":"assistant","content":"READ_OK" if continuing else None,"reasoning_content":FINAL_REASON if continuing else REASON}
                if not continuing:
                    message["tool_calls"]=CALLS
                return httpx2.Response(200,json=response(message,"stop" if continuing else "tool_calls"))

            with patch("personal_agent.llm.OpenAI",side_effect=sdk(handle)):
                histories=[]
                for stream,name in ((False,"qwen-json"),(True,"qwen-stream")):
                    args=["--protocol","openai","--model",MODEL,"--thinking","enabled","--thinking-budget","512",
                        "--max-output-tokens","2048","--session",name,"--with-tools","--dont-ask","--deny-tool","write_file",
                        "--prompt","读取实际文件；写入应被拒绝。","--output-format","json"] + (["--stream"] if stream else [])
                    code,output,errors=invoke(args)
                    assert code==0,(code,errors)
                    report=json.loads(output)
                    assert report["answer"]=="READ_OK" and report["usage"]["calls"]==2 and report["tool_error_count"]==1
                    assert REASON not in output+errors and "synthetic-final" not in output+errors
                    history=Session(name).load(); histories.append(history)
                    assert json.loads(Session(name).path.read_text(encoding="utf-8"))["version"]==4
                    assert history[1]["qwen"]["reasoning_content"]==REASON and history[-1]["qwen"]["reasoning_content"]==FINAL_REASON
                    assert not (root/"playground/denied.txt").exists()
                saved=deepcopy(histories[0])
                assert all("qwen" not in item for item in without_provider_state(saved))
                assert REASON not in str(prepare_messages(saved,None,"",thinking={"type":"disabled"},model="other"))
                assert saved==histories[0]
                from personal_agent.llm import _prepare_request
                continuing=saved[:-1]
                reject(lambda:_prepare_request(continuing,None,{"model":MODEL}))
                ordinary,_=_prepare_request(saved+[{"role":"user","content":"new task"}],None,{"model":"other"})
                assert REASON not in str(ordinary) and "qwen" not in str(ordinary)
            for overrides in ({**OPTIONS,"base_url":"https://example.invalid/v1"},{**OPTIONS,"model":"qwen-unknown"},
                    {**OPTIONS,"thinking":"adaptive"},{**OPTIONS,"thinking_budget":2048}):
                reject(lambda: model_config_scope(overrides).__enter__())
            bad=deepcopy(saved[1]); bad["qwen"]["reasoning_content"]=123
            reject(lambda:validate_state(bad))
            with model_config_scope(OPTIONS):
                for deltas,finish in (([{"reasoning_content":123}],"stop"),([{ "content":"visible"},{"reasoning_content":"late"}],"stop"),
                        ([{"reasoning_content":"incomplete"}],"length")):
                    executed=[]
                    with patch("personal_agent.llm.OpenAI",side_effect=sdk(lambda request:sse(deltas,finish))):
                        reject(lambda:run_agent_loop([{"role":"user","content":"bad stream"}],
                            lambda messages,definitions:stream_model_message(messages,definitions),tool_executor=lambda *args:executed.append(args)))
                    assert not executed
            assert source.read_text(encoding="utf-8")=="ACTUAL_READ_EVIDENCE" and len(requests)==4
        process=subprocess.run([sys.executable,"-X","utf8","-B",str(Path(__file__).resolve()),"--inspect-root",str(root)],cwd=root,capture_output=True,timeout=60)
        assert process.returncode==0,process.stderr.decode("utf-8",errors="replace")
    return {"status":"passed","validation":"actual SDK with fixed HTTP/SSE; real CLI, file read/deny and Session v4/fresh process",
        "nonstream_and_stream_reasoning_roundtrip_tools_and_usage":True,"reasoning_separate_from_visible_output_and_other_protocols":True,
        "unsupported_profile_mid_task_switch_and_bad_stream_rejected_before_tools":True,"legacy_default_off_preserved":request_options({"model":"legacy"},default_max_tokens=256)=={"extra_body":{"enable_thinking":False},"max_tokens":256},
        "new_remote_model_calls":0}


def verify_live():
    """一次只读工具任务加一次流式短回复；不因失败重跑或切换型号。"""
    folder=ROOT/".personal/qwen-thinking-live"/uuid4().hex
    workspace=folder/"workspace"
    (workspace/"playground").mkdir(parents=True,exist_ok=False)
    marker="QWEN-LIVE-"+uuid4().hex[:12]
    source=workspace/"playground/probe.txt"
    source.write_text(marker+"\n",encoding="utf-8")
    original=source.read_bytes()
    stats,raw_usage,trace=UsageStats(),[],[]
    history=[{"role":"user","content":"请先调用 read_file 读取 playground/probe.txt，然后只回复该文件第一行的标识。不能猜测或读取其他路径。"}]
    report={"status":"running","validation":"real current Qwen API; actual read-only tool and separate streaming request",
        "directory":str(folder),"new_remote_model_logical_calls":0,"stream_status":"not_run","checks":{}}
    try:
        with model_config_scope({"protocol":"openai","thinking":"enabled","thinking_budget":256,"max_tokens":1024}) as settings:
            report["settings"]={key:settings[key] for key in ("protocol","model","thinking","max_tokens")}

            def tracked(call):
                reported=None
                def capture(value):
                    nonlocal reported
                    reported=value
                try:
                    return call(capture)
                finally:
                    stats.record(reported)
                    raw_usage.append(reported)

            def model(messages,definitions):
                return tracked(lambda capture:get_model_message(messages,[item for item in definitions if item["name"]=="read_file"],on_usage=capture))

            def execute(name,arguments):
                with tool_workspace(workspace):
                    result=execute_tool(name,arguments,mode="dontAsk",deny=["write_file","edit_file","run_shell"]) if name=="read_file" and arguments.get("file_path")=="playground/probe.txt" else "工具调用失败：探针只允许读取指定演示文件。"
                trace.append({"name":name,"arguments":arguments,"result_sha256":digest(result.encode("utf-8")),"read_marker_observed":marker in result})
                return result

            answer=run_agent_loop(history,model,max_turns=3,tool_executor=execute)
            validate_agent_messages(history)
            report["checks"]={"actual_tool_read":any(item["read_marker_observed"] for item in trace),"answer_contains_actual_marker":marker in answer,
                "nonstream_reasoning_observed":any(item.get("qwen",{}).get("reasoning_content") for item in history)}
            shown=[]
            streamed=tracked(lambda capture:stream_model_message([{"role":"user","content":"计算 13+29，只回复数字。"}],on_text=shown.append,on_usage=capture))
            report["stream_status"]="completed"
            report["checks"].update(stream_answer="".join(shown)==streamed["content"] and "42" in streamed["content"],
                stream_reasoning_observed=bool(streamed.get("qwen",{}).get("reasoning_content")),source_unchanged=source.read_bytes()==original,
                usage_reported_once_per_logical_call=stats.calls==stats.reported_calls)
            write_json_atomic(folder/"stream-message.json",streamed)
            report["status"]="passed" if all(report["checks"].values()) else "partial"
    except (Exception,KeyboardInterrupt) as error:
        report.update(status="cancelled" if isinstance(error,KeyboardInterrupt) else "failed",error_type=type(error).__name__)
        if getattr(error,"status_code",None) is not None:
            report["status_code"]=error.status_code
    write_json_atomic(folder/"history.json",history)
    write_json_atomic(folder/"tool-trace.json",trace)
    report.update(new_remote_model_logical_calls=stats.calls,usage=vars(stats),provider_usage=raw_usage,
        reasoning_lengths=[len(item["qwen"]["reasoning_content"]) for item in history if "qwen" in item],
        history_complete=report["status"] in {"passed","partial"},source_sha256=digest(original),source_unchanged=source.read_bytes()==original,
        boundary="one authored read-only case and one streaming arithmetic request; not Anthropic/signature acceptance, broad quality or generic provider negotiation")
    write_json_atomic(folder/"report.json",report)
    write_json_atomic(ROOT/"codex/qwen-thinking-live-check-2026-10-31.json",report)
    return report


if __name__=="__main__":
    if sys.argv[1:2]==["--inspect-root"]:
        print(json.dumps(inspect_restore(Path(sys.argv[2])),ensure_ascii=False))
    elif sys.argv[1:]==["--live"]:
        print(json.dumps(verify_live(),ensure_ascii=False))
    else:
        print(json.dumps(verify_workflow(),ensure_ascii=False))
