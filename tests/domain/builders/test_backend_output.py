import copy

from patient360.domain.builders.ai_input import build_ai_input
from patient360.domain.builders.backend_output import build_backend_output
from patient360.domain.rules.packages import Package, package_entries

AI = {"summary": {"en": "s", "ar": "س"}, "recommendations": []}


def test_the_output_has_ids_status_and_alert_per_parameter(sample):
    raw = sample("batch_critical")
    rows = build_ai_input(raw).rows
    out = build_backend_output(
        patient_id=raw["patient_id"], visit_id=raw["visit_id"], source=raw["source"], rows=rows, ai=AI)
    assert (out["patient_id"], out["visit_id"], out["source"]) == (raw["patient_id"], raw["visit_id"], 2)
    results = [r for t in out["tests"] for r in t["results"]]
    calcium = next(r for r in results if r["status"] == "critical" and r["alert"] == "CRITICAL")
    assert set(calcium) == {"parameter_id", "status", "alert"}
    assert [t["test_id"] for t in out["tests"]] == list(dict.fromkeys(r["test_id"] for r in rows))


def test_a_result_without_an_alert_has_no_alert_key():
    rows = [{"test_id": 1, "parameter_id": 5, "status": "text", "alert": None}]
    out = build_backend_output(patient_id=1, visit_id=2, source=1, rows=rows, ai=AI)
    assert out["tests"] == [{"test_id": 1, "results": [{"parameter_id": 5, "status": "text"}]}]


def test_results_are_grouped_by_test_in_the_order_they_came():
    rows = [{"test_id": 1, "parameter_id": 5, "status": "normal", "alert": "NORMAL"},
            {"test_id": 2, "parameter_id": 6, "status": "high", "alert": "MEDICAL_ATTENTION"},
            {"test_id": 1, "parameter_id": 7, "status": "low", "alert": "MEDICAL_ATTENTION"}]
    out = build_backend_output(patient_id=1, visit_id=2, source=1, rows=rows, ai=AI)
    assert [(t["test_id"], [r["parameter_id"] for r in t["results"]]) for t in out["tests"]] == [(1, [5, 7]), (2, [6])]


def test_packages_and_html_are_added_and_the_ai_input_is_not_changed():
    before = copy.deepcopy(AI)
    packages = package_entries([Package(1, "Kidney check", 3, None, None, frozenset({1, 2}))],
                               {1: "Urea", 2: "Creatinine"})
    out = build_backend_output(patient_id=1, visit_id=2, source=1, rows=[], ai=AI, packages=packages)
    assert out["ai"]["packages"] == [{"package_name": "Kidney check",
                                      "tests": [{"test_name": "Urea"}, {"test_name": "Creatinine"}]}]
    assert out["html"]["en"].startswith('<div lang="en">') and AI == before
    assert "packages" not in build_backend_output(patient_id=1, visit_id=2, source=1, rows=[], ai=AI)["ai"]


def test_the_symptom_result_has_the_same_shape(sample):
    rows = build_ai_input(sample("batch_critical")).rows
    out = build_backend_output(patient_id=1, visit_id=2, source=3, rows=rows, ai=AI)
    assert out["source"] == 3 and set(out) == {"patient_id", "visit_id", "source", "tests", "ai", "html"}


def test_descriptive_results_are_listed_with_their_status_only_when_there_are_some():
    descriptive = [{"test_id": 7, "test_name": "PCR", "status": "text"}]
    out = build_backend_output(patient_id=1, visit_id=2, source=1, rows=[], descriptive_rows=descriptive, ai=AI)
    assert out["descriptive_results"] == [{"test_id": 7, "status": "text"}]
    assert "descriptive_results" not in build_backend_output(patient_id=1, visit_id=2, source=1, rows=[], ai=AI)


def test_the_arabic_test_names_come_from_the_database_names_given():
    ai = {"summary": {"en": "s", "ar": "ص"}, "recommendations": [{"test_name": "Urea"}]}
    names = {"Urea": "اليوريا"}
    out = build_backend_output(patient_id=1, visit_id=2, source=1, rows=[], ai=ai, arabic_test_names=names)
    assert "<li>اليوريا</li>" in out["html"]["ar"] and "<li>Urea</li>" in out["html"]["en"]


def test_a_given_html_replaces_the_built_one():
    given = {"en": "<p>x</p>", "ar": "<p>y</p>"}
    out = build_backend_output(patient_id=1, visit_id=2, source=1, rows=[], ai=AI, html=given)
    assert out["html"] == {"en": "<p>x</p>", "ar": "<p>y</p>"}
