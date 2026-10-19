"""Backend payload -> AI document (flow "2a", before RAG). Pure: no I/O, never mutates its input."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from patient360.domain.logic import (
    alert_level_for,
    classify_qualitative,
    classify_result,
    critical_bounds,
    critical_breach,
    lab_flag_status,
    parse_text_range,
    reference_bounds,
)
from patient360.domain.values import remove_empty_values

LAB_FLAG_WORDS = {"H": "High", "L": "Low"}


@dataclass(frozen=True)
class ProcessedVisit:
    document: dict[str, Any]     # what goes to AWS (before RAG): keys added/removed, empty values removed
    rows: list[dict[str, Any]]   # one row per parameter, for decide_tier and the lab_results table


def _result(raw: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    ref = raw.get("reference") or {}
    value = raw.get("value")
    low, high = reference_bounds(ref.get("low"), ref.get("high"))
    # The document shows the Backend's own low/high; a LIMS "not set" pair (0/0, -1/9999) is dropped.
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
        bounds = {"low": crit_low, "high": crit_high}
        # "no limit" marker pairs are left out of the document; any other limit is shown as sent
        has_limits = (crit_low, crit_high) != (None, None)
        shown_critical = {
            "low": critical.get("low") if has_limits else None,
            "high": critical.get("high") if has_limits else None,
            "range_text": critical.get("range_text"),
        }
        direction = critical_breach(value, bounds, status=status, ref_low=low, ref_high=high)
    out = {
        "parameter_name": raw.get("parameter_name"),
        "result_date": raw.get("result_date"),
        "value": value,
        "unit": raw.get("unit"),
        "reference": {"low": shown_low, "high": shown_high, "range": ref.get("range")},
        "status": status,
        "lab_flag": LAB_FLAG_WORDS.get(str(raw.get("lab_flag") or "").strip().upper()),
        "alert": alert_level_for(status, direction),
        "critical": {**shown_critical, "direction": f"critical_{direction}" if direction else None}
        if shown_critical else None,
        "previous_value": raw.get("previous_value"),
        "previous_date": raw.get("previous_date"),
        "parameter_remark": raw.get("parameter_remark"),
        "history": [
            {"date": h.get("date"), "value": h.get("value")}
            for h in (raw.get("history") or [])
        ],
    }
    flat = {
        "parameter_name": raw.get("parameter_name"), "value": value,
        "status": status, "is_critical": bool(direction),
    }
    return out, flat


def build_visit_document(raw: Mapping[str, Any]) -> ProcessedVisit:
    """Remove keys (ids, company name, nulls), add keys (status, alert, direction, mapped flag...)."""
    flat: list[dict[str, Any]] = []
    tests = []
    for test in raw.get("tests") or []:
        results = []
        for item in test.get("results") or []:
            out, row = _result(item)
            results.append(out)
            flat.append(row)
        tests.append({
            "test_name": test.get("test_name"),
            "test_remark": test.get("test_remark"),
            "results": results,
        })
    doc = remove_empty_values({
        "source": raw.get("source"),
        "patient": raw.get("patient"),
        "tests": tests,
        "descriptive_results": raw.get("descriptive_results"),
        "visitAnswerList": raw.get("visitAnswerList"),
    }) or {}
    return ProcessedVisit(document=doc, rows=flat)
