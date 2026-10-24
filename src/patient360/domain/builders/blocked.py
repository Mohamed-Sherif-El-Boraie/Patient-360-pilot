"""A result the guardrail blocked: the Backend gets one fixed message and nothing the model wrote.

Two messages only: the guardrail refused what we sent (input), or it refused what the model wrote (output). Nothing from
the trace (matched words, topic names, personal data) is ever copied into a message.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

GUARDRAIL_ACTION_KEY = "amazon-bedrock-guardrailAction"
TRACE_KEY = "amazon-bedrock-trace"

BLOCKED_MESSAGES = {
    "input": {
        "en": "We could not process some of the information provided because it contains content that is not allowed.",
        "ar": "تعذّرت معالجة بعض المعلومات المُدخلة لأنها تحتوي على محتوى غير مسموح به.",
    },
    "output": {
        "en": "We had a problem on our side while preparing your summary.",
        "ar": "حدثت مشكلة من جانبنا أثناء إعداد الملخص الخاص بك.",
    },
}


def _guardrail_action(record: Mapping[str, Any]) -> Any:
    """The action key is at the top of the record; some shapes keep it inside ``metadata`` per language."""
    if GUARDRAIL_ACTION_KEY in record:
        return record[GUARDRAIL_ACTION_KEY]
    for part in (record.get("metadata") or {}).values():
        if isinstance(part, Mapping) and GUARDRAIL_ACTION_KEY in part:
            return part[GUARDRAIL_ACTION_KEY]
    return None


def _actions(node: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], str]]:
    """Every ``action`` in the trace (BLOCKED, ANONYMIZED, ...) with the keys leading to it."""
    if isinstance(node, Mapping):
        if isinstance(node.get("action"), str):
            yield path, node["action"]
        for key, child in node.items():
            yield from _actions(child, (*path, str(key)))
    elif isinstance(node, list):
        for child in node:
            yield from _actions(child, path)


def is_blocked(record: Mapping[str, Any]) -> bool:
    """INTERVENED is also reported when the guardrail only replaced personal data (ANONYMIZED) and the answer is still
    good. So with a trace that shows no BLOCKED action the answer is used; with no usable trace we play safe."""
    if _guardrail_action(record) != "INTERVENED":
        return False
    actions = {action for _, action in _actions(record.get(TRACE_KEY) or {})}
    return not actions or "BLOCKED" in actions


def blocked_reason(record: Mapping[str, Any]) -> str:
    """``input`` when the guardrail refused what we sent (a key such as ``input`` or ``inputAssessment`` leads to the
    block), otherwise ``output``."""
    for path, action in _actions(record.get(TRACE_KEY) or {}):
        if action == "BLOCKED" and any(key.lower().startswith("input") for key in path):
            return "input"
    return "output"


def blocked_ai(record: Mapping[str, Any]) -> dict[str, Any]:
    """The ``ai`` part of a blocked result: only the summary is filled, the rest is null."""
    return {"summary": dict(BLOCKED_MESSAGES[blocked_reason(record)]), "simple_summary": None,
            "critical_alerts": None, "note": None, "recommendations": None}
