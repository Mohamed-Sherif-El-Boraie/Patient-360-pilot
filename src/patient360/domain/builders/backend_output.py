"""The result sent to the Backend: ids, each parameter's status and alert, the AI answer, packages and html."""
from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from patient360.domain.builders.html_report import build_html

SENT_ALERTS = ("MEDICAL_ATTENTION", "CRITICAL")   # an alert exists only for low, high, abnormal and critical results


def build_backend_output(
    *, patient_id: int, visit_id: int, source: int, rows: Sequence[Mapping[str, Any]],
    ai: Mapping[str, Any], packages: Sequence[Mapping[str, Any]] | None = None, html: Mapping[str, Any] | None = None,
    arabic_test_names: Mapping[str, str] | None = None, html_reasons: bool = False, symptom_flag: bool = False,
) -> dict[str, Any]:
    """``rows`` come from ``build_ai_input``. Descriptive results are not part of this result.
    ``alert`` is MEDICAL_ATTENTION or CRITICAL (low, high, abnormal, critical results), else ``null``: the key is always
    there, also for normal and text results.
    ``symptom_flag`` tells the Backend if the patient may use the symptom checker on this visit (true) or it is
    blocked (false).
    ``html`` is built from the AI answer (Arabic test names from ``arabic_test_names``) unless one is given."""
    tests: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        alert = row.get("alert") if row.get("alert") in SENT_ALERTS else None      # normal and text: no alert
        result = {"parameter_id": row["parameter_id"], "status": row["status"], "alert": alert}
        tests.setdefault(row["test_id"], []).append(result)
    ai_out = copy.deepcopy(dict(ai))
    if packages is not None:
        ai_out["packages"] = [dict(p) for p in packages]
    output = {
        "patient_id": patient_id,
        "visit_id": visit_id,
        "source": source,
        "symptom_flag": symptom_flag,
        "tests": [{"test_id": test_id, "results": results} for test_id, results in tests.items()],
    }
    output["ai"] = ai_out
    output["html"] = dict(html) if html is not None else build_html(ai_out, arabic_test_names, html_reasons)
    return output
