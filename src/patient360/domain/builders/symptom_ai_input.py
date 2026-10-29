"""Symptom-checker JSON for the AI model: the patient's words, the stored visit and its English AI answer.

The stored AI answer is sent to Bedrock under the key ``previous_ai_analysis`` (without its packages).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from patient360.domain.builders.ai_input import SOURCE_NAMES
from patient360.domain.payload.values import remove_empty_values


def keep_english_only(node: Any) -> Any:
    """Collapse every bilingual text ``{"en": ..., "ar": ...}`` to its English string; other keys are kept."""
    if isinstance(node, dict):
        if "en" in node and "ar" in node:
            return node["en"]
        return {key: keep_english_only(value) for key, value in node.items()}
    if isinstance(node, list):
        return [keep_english_only(item) for item in node]
    return node


def build_symptom_ai_input(
    request: Mapping[str, Any], stored_visit_ai_json: Mapping[str, Any], stored_ai: Mapping[str, Any],
    received_at: str | None = None,
) -> dict[str, Any]:
    """``request`` is the validated symptom payload, ``stored_visit_ai_json`` the JSON the visit sent to the AI model,
    ``stored_ai`` the ``ai`` object of the visit's result (summary, note, recommendations...), ``received_at`` the UTC
    date/time the symptoms were received: the model weighs it against each ``result_date``. The prompt must name the key
    ``symptom_received_at``."""
    previous = {key: value for key, value in stored_ai.items() if key != "packages"}
    return remove_empty_values({
        "source": SOURCE_NAMES.get(request["source"]),
        "patient_symptoms": request["patient_symptoms"],
        "symptom_received_at": received_at,
        "patient": stored_visit_ai_json.get("patient"),
        "tests": stored_visit_ai_json.get("tests"),
        "descriptive_results": stored_visit_ai_json.get("descriptive_results"),
        "visit_answer_list": stored_visit_ai_json.get("visit_answer_list"),
        "previous_ai_analysis": keep_english_only(previous),
    }) or {}
