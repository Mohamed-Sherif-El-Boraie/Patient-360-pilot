import copy

from patient360.domain.builders.ai_input import build_ai_input
from patient360.domain.builders.backend_output import build_backend_output, build_failed_output, for_wrapper
from patient360.domain.rules.packages import Package, package_entries

AI = {"summary": {"en": "s", "ar": "س"}, "recommendations": []}


STATUSES = {"normal", "high", "low", "abnormal", "text", "critical"}      # the owner's list: nothing else is sent


def row(test_id, parameter_id, status, unit=None, critical_direction=None, history=()):
    return {"test_id": test_id, "parameter_id": parameter_id, "value": "7.5", "status": status,
            "result_date": "2026-02-01", "unit": unit, "critical_direction": critical_direction,
            "history": list(history)}


def test_the_output_has_ids_status_unit_direction_and_history_per_parameter_and_no_alert(sample):
    raw = sample("batch_critical")
    rows = build_ai_input(raw).rows
    out = build_backend_output(
        patient_id=raw["patient_id"], visit_id=raw["visit_id"], source=raw["source"], rows=rows, ai=AI)
    assert (out["patient_id"], out["visit_id"], out["source"]) == (raw["patient_id"], raw["visit_id"], 2)
    results = [r for t in out["tests"] for r in t["results"]]
    calcium = next(r for r in results if r["status"] == "critical")
    assert list(calcium) == ["parameter_id", "value", "status", "result_date", "unit", "critical", "history"]
    assert calcium["critical"]["direction"] in ("critical_low", "critical_high")
    assert all(set(r["critical"]) == {"direction"} for r in results)      # always an object, never a flat key
    assert all("alert" not in r for r in results)
    assert [t["test_id"] for t in out["tests"]] == list(dict.fromkeys(r["test_id"] for r in rows))


def test_the_status_is_always_one_of_the_six_values_for_every_sample(sample):
    for name in ("batch_critical", "batch_normal", "urgent_mild", "on_demand_critical", "descriptive_only"):
        assert {r["status"] for r in build_ai_input(sample(name)).rows} <= STATUSES


def test_a_history_point_gets_the_unit_of_its_result_and_no_history_is_an_empty_list():
    points = [{"history_date": "2026-01-01", "history_value": "5.1"},
              {"history_date": "2025-06-01", "history_value": "4"}]
    rows = [row(1, 5, "high", "mg/dL", "critical_high", points), row(1, 6, "normal")]
    out = build_backend_output(patient_id=1, visit_id=2, source=1, rows=rows, ai=AI)
    first, second = out["tests"][0]["results"]
    assert (first["unit"], first["critical"]) == ("mg/dL", {"direction": "critical_high"})
    assert first["history"] == [{"date": "2026-01-01", "value": "5.1", "unit": "mg/dL"},
                                {"date": "2025-06-01", "value": "4", "unit": "mg/dL"}]
    assert (second["unit"], second["critical"], second["history"]) == (None, {"direction": None}, [])


def test_the_html_is_built_from_the_full_answer_and_the_summary_stays_in_the_saved_result():
    ai = {"summary": {"en": "SUMMARY-TEXT", "ar": "x"}, "critical_alerts": [{"en": "ALERT-TEXT", "ar": "y"}],
          "recommendations": []}
    out = build_backend_output(patient_id=1, visit_id=2, source=1, rows=[], ai=ai)
    assert "SUMMARY-TEXT" in out["html"]["en"]
    assert out["ai"]["summary"] and out["ai"]["critical_alerts"]
    sent = for_wrapper(out)
    assert set(sent["ai"]) == {"recommendations"} and sent["html"] == out["html"]
    assert out["ai"]["summary"]      # the saved result is not changed


def test_for_wrapper_drops_the_three_keys_for_a_visit_and_keeps_everything_else():
    ai = {"summary": AI["summary"], "simple_summary": AI["summary"], "critical_alerts": [], "note": AI["summary"],
          "recommendations": []}
    out = build_backend_output(patient_id=1, visit_id=2, source=1, rows=[row(1, 5, "normal")], ai=ai, packages=[])
    sent = for_wrapper(out)
    assert list(sent) == ["success", "patient_id", "visit_id", "source", "symptom_flag", "tests", "ai", "html"]
    assert set(sent["ai"]) == {"note", "recommendations", "packages"} and sent["tests"] == out["tests"]


def test_a_symptom_result_is_sent_with_the_flag_and_packages_but_no_tests():
    ai = {"summary": AI["summary"], "simple_summary": AI["summary"], "critical_alerts": [], "note": AI["summary"],
          "recommendations": []}
    out = build_backend_output(patient_id=1, visit_id=2, source=3, rows=[row(1, 5, "normal")], ai=ai, packages=[],
                               symptom_flag=True)
    sent = for_wrapper(out)
    assert list(sent) == ["success", "patient_id", "visit_id", "source", "symptom_flag", "ai", "html"]
    assert sent["symptom_flag"] is True and list(sent["ai"]) == ["note", "recommendations", "packages"]


def test_a_blocked_result_is_sent_with_its_fixed_message_empty_recommendations_and_no_packages():
    blocked = {"summary": {"en": "fixed", "ar": "ثابت"}, "simple_summary": None, "critical_alerts": None,
               "note": None, "recommendations": None}
    visit = for_wrapper(build_backend_output(patient_id=1, visit_id=2, source=1, rows=[row(1, 5, "high")], ai=blocked))
    assert visit["ai"] == {"note": {"en": "fixed", "ar": "ثابت"}, "recommendations": [], "packages": []}
    assert visit["symptom_flag"] is False and visit["tests"]
    symptom = for_wrapper(build_backend_output(patient_id=1, visit_id=2, source=3, rows=[], ai=blocked,
                                               symptom_flag=True))
    assert symptom["ai"] == visit["ai"] and symptom["symptom_flag"] is True and "tests" not in symptom


def test_a_failed_result_is_sent_as_it_is():
    failed = build_failed_output(patient_id=1, visit_id=2, source=3)
    assert for_wrapper(failed) == failed


def test_results_are_grouped_by_test_in_the_order_they_came():
    rows = [row(1, 5, "normal"), row(2, 6, "high"), row(1, 7, "low")]
    out = build_backend_output(patient_id=1, visit_id=2, source=1, rows=rows, ai=AI)
    assert [(t["test_id"], [r["parameter_id"] for r in t["results"]]) for t in out["tests"]] == [(1, [5, 7]), (2, [6])]


def test_packages_and_html_are_added_and_the_ai_input_is_not_changed():
    before = copy.deepcopy(AI)
    packages = package_entries([Package(1, "Kidney check", 3, None, None, frozenset({1, 2}), "فحص الكلى")],
                               {1: ("Urea", "اليوريا"), 2: ("Creatinine", "الكرياتينين")})
    out = build_backend_output(patient_id=1, visit_id=2, source=1, rows=[], ai=AI, packages=packages)
    assert out["ai"]["packages"] == [{
        "package_name": {"en": "Kidney check", "ar": "فحص الكلى"},
        "tests": [{"test_name": {"en": "Urea", "ar": "اليوريا"}},
                  {"test_name": {"en": "Creatinine", "ar": "الكرياتينين"}}]}]
    assert out["html"]["en"].startswith('<div lang="en">') and AI == before
    assert "packages" not in build_backend_output(patient_id=1, visit_id=2, source=1, rows=[], ai=AI)["ai"]


def test_the_symptom_result_has_the_same_shape(sample):
    rows = build_ai_input(sample("batch_critical")).rows
    out = build_backend_output(patient_id=1, visit_id=2, source=3, rows=rows, ai=AI)
    keys = {"success", "patient_id", "visit_id", "source", "symptom_flag", "tests", "ai", "html"}
    assert out["source"] == 3 and set(out) == keys and out["success"] is True


def test_the_failed_output_has_the_ids_success_false_and_everything_else_null():
    assert build_failed_output(patient_id=1, visit_id=2, source=4) == {
        "success": False, "patient_id": 1, "visit_id": 2, "source": 4,
        "symptom_flag": None, "tests": None, "ai": None, "html": None}


def test_the_symptom_flag_is_always_in_the_result_and_is_false_unless_it_is_given_as_true():
    given = build_backend_output(patient_id=1, visit_id=2, source=1, rows=[], ai=AI, symptom_flag=True)
    assert given["symptom_flag"] is True
    assert build_backend_output(patient_id=1, visit_id=2, source=1, rows=[], ai=AI)["symptom_flag"] is False


def test_the_result_has_no_descriptive_results_and_no_symptom_keys_for_a_visit(sample):
    rows = build_ai_input(sample("descriptive_only")).rows
    out = build_backend_output(patient_id=1, visit_id=2, source=1, rows=rows, ai=AI)
    assert "descriptive_results" not in out
    assert all("change_type" not in r and "explanation" not in r for r in out["ai"]["recommendations"])


def test_the_arabic_test_names_come_from_the_database_names_given():
    ai = {"summary": {"en": "s", "ar": "ص"}, "recommendations": [{"test_name": "Urea"}]}
    names = {"Urea": "اليوريا"}
    out = build_backend_output(patient_id=1, visit_id=2, source=1, rows=[], ai=ai, arabic_test_names=names)
    assert "<li>اليوريا</li>" in out["html"]["ar"] and "<li>Urea</li>" in out["html"]["en"]


def test_a_given_html_replaces_the_built_one():
    given = {"en": "<p>x</p>", "ar": "<p>y</p>"}
    out = build_backend_output(patient_id=1, visit_id=2, source=1, rows=[], ai=AI, html=given)
    assert out["html"] == {"en": "<p>x</p>", "ar": "<p>y</p>"}
