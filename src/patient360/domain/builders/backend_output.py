"""The result sent to the Backend: ids, each parameter's value, status, date, unit, critical direction and history, the
AI answer, packages and html."""
from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from patient360.domain.builders.blocked import is_blocked_ai
from patient360.domain.builders.html_report import build_html

SYMPTOM_SOURCE = 3
SYMPTOM_AI_KEYS = ("note", "recommendations", "packages")      # what a symptom-check result sends in ``ai``


def group_by_test(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    tests: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        result = {"parameter_id": row["parameter_id"], "value": row["value"], "status": row["status"],
                  "result_date": row["result_date"], "unit": row["unit"],
                  "critical": {"low": None, "high": None, "range_text": None, **(row.get("critical") or {}),
                               "direction": row["critical_direction"]},
                  "history": [{"date": p["history_date"], "value": p["history_value"], "unit": row["unit"]}
                              for p in row["history"]]}
        tests.setdefault(row["test_id"], []).append(result)
    return [{"test_id": test_id, "results": results} for test_id, results in tests.items()]


def build_backend_output(
    *, patient_id: int, visit_id: int, source: int, rows: Sequence[Mapping[str, Any]],
    ai: Mapping[str, Any], packages: Sequence[Mapping[str, Any]] | None = None, html: Mapping[str, Any] | None = None,
    arabic_test_names: Mapping[str, str] | None = None, html_reasons: bool = False, symptom_flag: bool = False,
    html_symptom: tuple[str, str] | None = None, company_id: int | None = None,
) -> dict[str, Any]:
    """``rows`` come from ``build_ai_input``. Descriptive results are not part of this result.
    ``status`` is one of normal, high, low, abnormal, text, critical. ``history`` points carry the result's unit.
    This is the FULL result that is saved (the symptom check reads its ``ai``); ``for_wrapper`` makes the body sent.
    ``symptom_flag`` tells the Backend if the patient may use the symptom checker on this visit (true) or it is
    blocked (false).
    ``html`` is built from the AI answer (Arabic test names from ``arabic_test_names``) unless one is given.
    ``html_symptom`` = (symptom text, yyyy-MM-dd date) for the symptom check's message in the html."""
    ai_out = copy.deepcopy(dict(ai))
    if packages is not None:
        ai_out["packages"] = [dict(p) for p in packages]
    output = {
        "success": True,
        "patient_id": patient_id,
        "visit_id": visit_id,
        "company_id": company_id,
        "source": source,
        "symptom_flag": symptom_flag,
        "tests": group_by_test(rows),
    }
    output["ai"] = ai_out
    output["html"] = (dict(html) if html is not None
                      else build_html(ai_out, arabic_test_names, html_reasons, html_symptom))
    return output


def critical_tests(tests: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Only the parameters with a critical direction, and only the tests that have one: [] when there is none."""
    kept = [{"Testid": t["test_id"],
             "results": [{"parameterid": r["parameter_id"], "critical": r["critical"]}
                         for r in t["results"] if r["critical"]["direction"]]} for t in tests]
    return [t for t in kept if t["results"]]


def for_wrapper(output: Mapping[str, Any]) -> dict[str, Any]:
    """The body the Backend gets, made from the saved result right before sending. A visit (urgent, on-demand, batch)
    is sent whole: ids, ``symptom_flag``, ``criticalTests`` (see ``critical_tests``), ``ai`` (summary, simple_summary,
    critical_alerts, note, recommendations, packages) and ``html``. A symptom check has no tests and ``ai`` = note,
    recommendations, packages (and the html). A blocked result carries its fixed message in the text keys and empty
    lists, so ``ai`` has the same keys as always. A failed result is sent as it is."""
    if not output.get("success"):
        return dict(output)
    full = output["ai"]
    blocked = is_blocked_ai(full)
    message = full["summary"]
    body = {("criticalTests" if k == "tests" else k): (critical_tests(v) if k == "tests" else v)
            for k, v in output.items() if k not in ("ai", "html")}
    if output["source"] == SYMPTOM_SOURCE:
        body.pop("criticalTests", None)
        body["ai"] = ({"note": message, "recommendations": [], "packages": []} if blocked
                      else {k: full[k] for k in SYMPTOM_AI_KEYS if k in full})
    else:
        body["ai"] = ({"summary": message, "simple_summary": message, "critical_alerts": [], "note": message,
                       "recommendations": [], "packages": []} if blocked else dict(full))
    body["html"] = output["html"]
    return body


def build_failed_output(
    *, patient_id: int, visit_id: int, source: int, company_id: int | None = None,
    rows: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """The message for a patient whose analysis failed: ``success`` false, the ids so the Backend knows who, the
    ``criticalTests`` of the visit's ``rows`` ([] when none, or when the visit is unknown), and every other key null.
    A failed symptom check has no ``criticalTests``, as a symptom result has none."""
    body = {"success": False, "patient_id": patient_id, "visit_id": visit_id, "company_id": company_id,
            "source": source, "symptom_flag": None, "criticalTests": critical_tests(group_by_test(rows)),
            "ai": None, "html": None}
    if source == SYMPTOM_SOURCE:
        del body["criticalTests"]
    return body
