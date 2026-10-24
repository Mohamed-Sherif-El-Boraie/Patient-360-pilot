"""A result the guardrail blocked: the Backend gets one fixed message and nothing the model wrote.

The message depends on WHY it was blocked, read from the guardrail trace of the AWS record. Only the kind of reason is
used. Nothing from the trace text (matched words, topic names, personal data) is ever copied into the message.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

GUARDRAIL_ACTION_KEY = "amazon-bedrock-guardrailAction"
TRACE_KEY = "amazon-bedrock-trace"

BLOCKED_MESSAGES = {
    "default": {
        "en": "The AI summary for this result could not be shown because it did not pass our safety checks. "
              "Please speak with your doctor about your results.",
        "ar": "تعذّر عرض الملخص الذكي لهذه النتيجة لأنه لم يجتز فحوصات السلامة لدينا. "
              "يرجى مراجعة طبيبك بخصوص نتائجك.",
    },
    "input": {
        "en": "We could not process some of the information provided because it contains content that is not "
              "allowed. Please remove it and try again, or speak with your doctor about your results.",
        "ar": "تعذّرت معالجة بعض المعلومات المُدخلة لأنها تحتوي على محتوى غير مسموح به. "
              "يرجى حذفه والمحاولة مرة أخرى، أو مراجعة طبيبك بخصوص نتائجك.",
    },
    "sensitive_information": {
        "en": "The AI summary for this result could not be shown because it would have contained personal "
              "information. Please speak with your doctor about your results.",
        "ar": "تعذّر عرض الملخص الذكي لهذه النتيجة لأنه كان سيحتوي على معلومات شخصية. "
              "يرجى مراجعة طبيبك بخصوص نتائجك.",
    },
    "ungrounded": {
        "en": "The AI summary for this result could not be shown because it could not be confirmed against your "
              "lab results. Please speak with your doctor about your results.",
        "ar": "تعذّر عرض الملخص الذكي لهذه النتيجة لأنه لم يمكن التحقق منه مقابل نتائج التحاليل. "
              "يرجى مراجعة طبيبك بخصوص نتائجك.",
    },
}
_REASON_BY_POLICY = {"sensitiveInformationPolicy": "sensitive_information", "contextualGroundingPolicy": "ungrounded"}


def _guardrail_action(record: Mapping[str, Any]) -> Any:
    """The action key is at the top of the record; some shapes keep it inside ``metadata`` per language."""
    if GUARDRAIL_ACTION_KEY in record:
        return record[GUARDRAIL_ACTION_KEY]
    for part in (record.get("metadata") or {}).values():
        if isinstance(part, Mapping) and GUARDRAIL_ACTION_KEY in part:
            return part[GUARDRAIL_ACTION_KEY]
    return None


def is_blocked(record: Mapping[str, Any]) -> bool:
    return _guardrail_action(record) == "INTERVENED"


def _blocked_paths(node: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[str, ...]]:
    """Every place in the trace where something has the action BLOCKED, as the keys leading to it."""
    if isinstance(node, Mapping):
        if node.get("action") == "BLOCKED":
            yield path
        for key, child in node.items():
            yield from _blocked_paths(child, (*path, str(key)))
    elif isinstance(node, list):
        for child in node:
            yield from _blocked_paths(child, path)


def blocked_reason(record: Mapping[str, Any]) -> str:
    """``input`` (the text we sent was refused), ``sensitive_information``, ``ungrounded`` or ``default``."""
    trace = record.get(TRACE_KEY) or {}
    paths = list(_blocked_paths(trace))
    if any("input" in path for path in paths):
        return "input"
    for path in paths:
        for key in path:
            if key in _REASON_BY_POLICY:
                return _REASON_BY_POLICY[key]
    return "default"


def blocked_ai(record: Mapping[str, Any]) -> dict[str, Any]:
    """The ``ai`` part of a blocked result: only the summary is filled, the rest is null."""
    return {"summary": dict(BLOCKED_MESSAGES[blocked_reason(record)]), "simple_summary": None,
            "critical_alerts": None, "note": None, "recommendations": None}
