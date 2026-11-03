"""非流式终结请求的协议级 JSON Schema 与明确失败；不修写模型文本。"""

from copy import deepcopy
import json


class StructuredResponseError(RuntimeError):
    def __init__(self, kind, message, content=None):
        super().__init__(kind)
        self.kind = kind
        self.raw_message = {"role": "assistant", "content": content if isinstance(content, str) else ""}
        self.reason = message


def checked_schema(schema, definitions):
    if schema is None:
        return None
    if definitions:
        raise ValueError("JSON Schema 终结请求不能同时开放工具。")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError("response_schema 必须是 object 根节点的 JSON Schema。")
    json.dumps(schema, allow_nan=False)
    return deepcopy(schema)


def _object(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def diagnosis_schema(failed, manifest, decision_version=1, *, rules=None):
    """只使用跨服务的基础结构类型；长度/证据/字面片段仍由本地检查。"""
    from .diagnosis_contract import RUN, FAILURES
    from .source_snapshot import PREFIX
    paths = [RUN, FAILURES, *(PREFIX + name for name in sorted(manifest))]
    if rules is not None:
        from .rule_evidence import RULES
        paths.append(RULES)
    python_paths = [PREFIX + name for name in sorted(manifest) if name.casefold().endswith(".py")]
    identifier = {"type": "string", "enum": sorted(failed)}
    text = {"type": "string"}
    citation = _object({"path": {"type": "string", "enum": paths}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}})
    evidence = {"type": "array", "items": citation}
    failure = _object({"test": identifier, "status": {"type": "string", "enum": ["suspected", "insufficient_evidence"]},
        "hypothesis": text, "recommendation": text, "evidence": evidence})
    if decision_version == 2:
        failure["properties"]["missing_information"] = {"type": "array", "items": text}
        failure["required"].append("missing_information")
    edit = _object({"path": {"type": "string", "enum": python_paths}, "old": text, "new": text,
        "addresses": {"type": "array", "items": identifier}, "evidence": evidence})
    if rules is not None:
        edit["properties"]["rule_ids"] = {"type": "array", "items": {"type": "string", "enum": sorted(rules["spans"])}}
        edit["required"].append("rule_ids")
    return _object({"summary": text, "failures": {"type": "array", "items": failure},
        "edits": {"type": "array", "items": edit}, "limitations": {"type": "array", "items": text}})


def strict_json(text):
    def pairs(items):
        result = {}
        for name, value in items:
            if name in result:
                raise ValueError("结构化诊断不接受重复 JSON 字段。")
            result[name] = value
        return result
    def constant(value):
        raise ValueError("结构化诊断不接受非标准 JSON 数值。")
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
