"""A result the guardrail blocked: the Backend gets one fixed message and nothing the model wrote."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

GUARDRAIL_ACTION_KEY = "amazon-bedrock-guardrailAction"
BLOCKED_SUMMARY = {
    "en": "The AI summary for this result could not be shown. Please speak with your doctor about your results.",
    "ar": "تعذّر عرض الملخص الذكي لهذه النتيجة. يرجى مراجعة طبيبك بخصوص نتائجك.",
}


def is_blocked(record: Mapping[str, Any]) -> bool:
    return record.get(GUARDRAIL_ACTION_KEY) == "INTERVENED"


def blocked_ai() -> dict[str, Any]:
    """The ``ai`` part of a blocked result: only the summary is filled, the rest is null."""
    return {"summary": dict(BLOCKED_SUMMARY), "simple_summary": None, "critical_alerts": None, "note": None,
            "recommendations": None}
