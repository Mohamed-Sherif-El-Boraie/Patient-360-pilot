"""The result sent to the Backend: ids, each parameter's value, status, date, unit, critical direction and history, the
AI answer, packages and html."""
from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from patient360.domain.builders.blocked import is_blocked_ai
from patient360.domain.builders.html_report import build_html

SYMPTOM_SOURCE = 3
NOT_SENT_AI_KEYS = ("summary", "simple_summary", "critical_alerts")      # used by the html and the symptom check only


def build_backend_output(
    *, patient_id: int, visit_id: int, source: int, rows: Sequence[Mapping[str, Any]],
    ai: Mapping[str, Any], packages: Sequence[Mapping[str, Any]] | None = None, html: Mapping[str, Any] | None = None,
    arabic_test_names: Mapping[str, str] | None = None, html_reasons: bool = False, symptom_flag: bool = False,
    html_symptom: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """``rows`` come from ``build_ai_input``. Descriptive results are not part of this result.
    ``status`` is one of normal, high, low, abnormal, text, critical. ``history`` points carry the result's unit.
    This is the FULL result that is saved (the symptom check reads its ``ai``); ``for_wrapper`` makes the body sent.
    ``symptom_flag`` tells the Backend if the patient may use the symptom checker on this visit (true) or it is
    blocked (false).
    ``html`` is built from the AI answer (Arabic test names from ``arabic_test_names``) unless one is given.
    ``html_symptom`` = (symptom text, yyyy-MM-dd date) for the symptom check's message in the html."""
    tests: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        result = {"parameter_id": row["parameter_id"], "value": row["value"], "status": row["status"],
                  "result_date": row["result_date"], "unit": row["unit"],
                  "critical": {"direction": row["critical_direction"]},
                  "history": [{"date": p["history_date"], "value": p["history_value"], "unit": row["unit"]}
                              for p in row["history"]]}
        tests.setdefault(row["test_id"], []).append(result)
    ai_out = copy.deepcopy(dict(ai))
    if packages is not None:
        ai_out["packages"] = [dict(p) for p in packages]
    output = {
        "success": True,
        "patient_id": patient_id,
        "visit_id": visit_id,
        "source": source,
        "symptom_flag": symptom_flag,
        "tests": [{"test_id": test_id, "results": results} for test_id, results in tests.items()],
    }
    output["ai"] = ai_out
    output["html"] = (dict(html) if html is not None
                      else build_html(ai_out, arabic_test_names, html_reasons, html_symptom))
    return output


def for_wrapper(output: Mapping[str, Any]) -> dict[str, Any]:
    """The body the Backend gets, made from the saved result right before sending: no summary, simple_summary or
    critical_alerts. A symptom-check result has no ``tests`` but keeps ``symptom_flag`` and ``packages``. A blocked
    result carries its fixed message as the note, with empty recommendations and packages, so ``ai`` has the same keys
    as always. A failed result is sent as it is."""
    if not output.get("success"):
        return dict(output)
    full = output["ai"]
    if is_blocked_ai(full):
        ai = {"note": full["summary"], "recommendations": [], "packages": []}
    else:
        ai = {k: v for k, v in full.items() if k not in NOT_SENT_AI_KEYS}
    body = {k: v for k, v in output.items() if k != "ai"}
    if output["source"] == SYMPTOM_SOURCE:
        body.pop("tests", None)
    body["ai"] = ai
    body["html"] = body.pop("html")      # html stays last
    return body


def build_failed_output(*, patient_id: int, visit_id: int, source: int) -> dict[str, Any]:
    """The message for a patient whose analysis failed: ``success`` false, the ids so the Backend knows who, and every
    other key null."""
    return {"success": False, "patient_id": patient_id, "visit_id": visit_id, "source": source,
            "symptom_flag": None, "tests": None, "ai": None, "html": None}
