"""Backend visit -> the JSON sent to the AI model, plus one row per parameter. Pure: no I/O, no mutation."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from patient360.domain.common.compat import frozendict
from patient360.domain.payload.age import age_in_days
from patient360.domain.payload.values import html_to_plain_text, remove_empty_values
from patient360.domain.rules.logic import (
    alert_level_for,
    classify_qualitative,
    classify_result,
    critical_bounds,
    critical_breach,
    lab_flag_status,
    parse_text_range,
    reference_bounds,
)

SOURCE_NAMES = frozendict({1: "urgent", 2: "batch", 3: "symptom-checker", 4: "on-demand"})
SEX_NAMES = frozendict({1: "Male", 2: "Female"})


@dataclass(frozen=True)
class AiInput:
    ai_json: dict[str, Any]               # the JSON sent to the AI model: no ids, no status; empty values removed
    rows: list[dict[str, Any]]            # one per parameter: ids, value, status, alert (for the tier and the output)
    descriptive_rows: list[dict[str, Any]]  # one per descriptive result: ids, status "text", the text without HTML
    performed_test_names: tuple[str, ...]  # every test the patient already did (numeric and descriptive)


def _history(points: Any, *, plain_text: bool = False) -> list[dict[str, Any]]:
    """History as the AI model gets it. ``plain_text``: the values are texts that may contain HTML tags."""
    return [{"history_date": p.get("history_date"),
             "history_value": html_to_plain_text(p.get("history_value")) if plain_text else p.get("history_value")}
            for p in points or []]


def _result(test: Mapping[str, Any], raw: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    ref = raw.get("reference") or {}
    value = raw.get("value")
    low, high = reference_bounds(ref.get("low"), ref.get("high"))
    # The JSON shows the Backend's own low/high; a LIMS "not set" pair (0/0, -1/9999) is dropped.
    shown_low, shown_high = (ref.get("low"), ref.get("high")) if (low, high) != (None, None) else (None, None)
    if low is None and high is None:   # numbers read from the range text are for classification only
        low, high = reference_bounds(*parse_text_range(ref.get("range")))
    status = (
        classify_result(value, low, high)
        or classify_qualitative(value, ref.get("range"))
        or lab_flag_status(raw.get("lab_flag"))
    )
    critical = raw.get("critical")
    direction = None
    shown_critical = None
    if critical:
        crit_low, crit_high = critical_bounds(critical.get("low"), critical.get("high"))
        has_limits = (crit_low, crit_high) != (None, None)   # a "no limit" marker pair is left out of the JSON
        shown_critical = {
            "low": critical.get("low") if has_limits else None,
            "high": critical.get("high") if has_limits else None,
            "range_text": critical.get("range_text"),
        }
        direction = critical_breach(
            value, {"low": crit_low, "high": crit_high}, status=status, ref_low=low, ref_high=high
        )
    if direction:
        status = "critical"
    status = status or "text"   # could not be judged: no range, no known word, no lab flag
    flag = lab_flag_status(raw.get("lab_flag"))
    alert = alert_level_for(status)
    out = {
        "parameter_name": raw.get("parameter_name"),
        "result_date": raw.get("result_date"),
        "value": value,
        "unit": raw.get("unit"),
        "reference": {"low": shown_low, "high": shown_high, "range": ref.get("range")},
        "alert": alert if alert != "NORMAL" else None,   # the model only gets CRITICAL or MEDICAL_ATTENTION
        "lab_flag": flag.capitalize() if flag else None,
        "critical": {**shown_critical, "direction": f"critical_{direction}" if direction else None}
        if shown_critical else None,
        "parameter_remark": raw.get("parameter_remark"),
        "history": _history(raw.get("history")),
    }
    row = {
        "test_id": test.get("test_id"), "test_name": test.get("test_name"),
        "parameter_id": raw.get("parameter_id"), "parameter_name": raw.get("parameter_name"),
        "value": value, "status": status, "alert": alert,
        "critical_direction": f"critical_{direction}" if direction else None,
    }
    return out, row


def build_ai_input(raw: Mapping[str, Any]) -> AiInput:
    """Build the JSON for the AI model from a validated visit: remove keys (ids, company), add keys (alert, direction,
    mapped flag, age in days), keep every value as sent."""
    rows: list[dict[str, Any]] = []
    tests = []
    for test in raw.get("tests") or []:
        results = []
        for item in test.get("results") or []:
            out, row = _result(test, item)
            results.append(out)
            rows.append(row)
        tests.append({"test_name": test.get("test_name"), "test_remark": test.get("test_remark"), "results": results})
    descriptive = [
        {"test_name": d.get("test_name"), "result_date": d.get("result_date"),
         "text": html_to_plain_text(d.get("text")), "history": _history(d.get("history"), plain_text=True)}
        for d in raw.get("descriptive_results") or []
    ]
    answers = [{"preparation_name": a.get("preparation_name"), "answer": a.get("answer")}
               for a in raw.get("visit_answer_list") or []]
    patient = raw.get("patient") or {}
    ai_json = remove_empty_values({
        "source": SOURCE_NAMES.get(raw.get("source")),
        "patient": {"age_days": age_in_days(patient["birthdate"]), "sex": SEX_NAMES.get(patient.get("sex"))},
        "tests": tests,
        "descriptive_results": descriptive,
        "visit_answer_list": answers,
    }) or {}
    performed = tuple(dict.fromkeys(
        [t.get("test_name") for t in raw.get("tests") or []]
        + [d.get("test_name") for d in raw.get("descriptive_results") or []]
    ))
    descriptive_rows = [{"test_id": d.get("test_id"), "test_name": d.get("test_name"), "status": "text",
                         "text_value": html_to_plain_text(d.get("text"))}
                        for d in raw.get("descriptive_results") or []]
    return AiInput(
        ai_json=ai_json, rows=rows, descriptive_rows=descriptive_rows, performed_test_names=performed)
