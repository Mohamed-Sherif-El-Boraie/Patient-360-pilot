import copy
from datetime import date, timedelta

import pytest

from patient360.domain.builders.ai_input import build_ai_input

FORBIDDEN_IN_THE_AI_INPUT = {"patient_id", "visit_id", "company_name", "test_id", "parameter_id", "status",
                                "history_visit_id", "previous_value", "previous_date", "tier"}


def keys_of(node, found=None):
    found = set() if found is None else found
    if isinstance(node, dict):
        for key, child in node.items():
            found.add(key)
            keys_of(child, found)
    elif isinstance(node, list):
        for child in node:
            keys_of(child, found)
    return found


ALL_VISITS = ["batch_normal", "batch_critical", "urgent_mild", "on_demand_critical", "descriptive_only"]


@pytest.mark.parametrize("name", ALL_VISITS)
def test_the_ai_input_has_no_ids_no_status_and_no_company(sample, name):
    ai_json = build_ai_input(sample(name)).ai_json
    assert not (keys_of(ai_json) & FORBIDDEN_IN_THE_AI_INPUT)


def test_the_ai_input_has_the_age_in_days_and_not_the_birthdate(sample):
    raw = sample("urgent_mild")
    raw["patient"]["birthdate"] = (date.today() - timedelta(days=28)).isoformat()
    patient = build_ai_input(raw).ai_json["patient"]
    assert patient["age_days"] == 28 and "birthdate" not in patient


def test_the_ai_input_names_the_source_and_the_sex(sample):
    ai_json = build_ai_input(sample("urgent_mild")).ai_json
    assert ai_json["source"] == "urgent" and ai_json["patient"]["sex"] == "Male"
    assert ai_json["patient"]["age_days"] > 365


def test_input_is_never_mutated(sample):
    raw = sample("batch_critical")
    before = copy.deepcopy(raw)
    build_ai_input(raw)
    assert raw == before


def test_critical_values_get_status_critical_and_alert_critical(sample):
    processed = build_ai_input(sample("batch_critical"))
    by_name = {row["parameter_name"]: row for row in processed.rows}
    assert (by_name["Calcium"]["status"], by_name["Calcium"]["alert"], by_name["Calcium"]["critical_direction"]) == (
        "critical", "CRITICAL", "critical_low")
    assert by_name["CREATININE (serum)"]["critical_direction"] == "critical_high"
    assert (by_name["Blood Urea"]["status"], by_name["Blood Urea"]["alert"]) == ("high", "MEDICAL_ATTENTION")
    calcium = next(r for t in processed.ai_json["tests"] for r in t["results"] if r["parameter_name"] == "Calcium")
    assert calcium["alert"] == "CRITICAL" and calcium["critical"]["direction"] == "critical_low"
    assert calcium["value"] == "5.3" and "status" not in calcium


def test_normal_values_get_alert_normal_and_no_direction(sample):
    processed = build_ai_input(sample("batch_normal"))
    assert {row["status"] for row in processed.rows} == {"normal"}
    assert {row["alert"] for row in processed.rows} == {"NORMAL"}
    results = [r for t in processed.ai_json["tests"] for r in t["results"]]
    creatinine = next(r for r in results if "CREATININE" in r["parameter_name"])
    assert "direction" not in creatinine.get("critical", {})


def row_for(sample, value, reference, critical=None, lab_flag=None):
    raw = sample("urgent_mild")
    raw["tests"] = [{"test_id": 1, "test_name": "T", "test_remark": None, "results": [{
        "parameter_id": 1, "parameter_name": "P", "result_date": "2026-10-20", "value": value, "unit": None,
        "reference": reference, "lab_flag": lab_flag, "critical": critical, "parameter_remark": None, "history": []}]}]
    processed = build_ai_input(raw)
    return processed.rows[0], processed.ai_json["tests"][0]["results"][0]


def test_a_result_that_cannot_be_judged_is_text_and_has_no_alert_key(sample):
    row, shown = row_for(sample, "see attached", {"low": None, "high": None, "range": None})
    assert (row["status"], row["alert"]) == ("text", None)
    assert "alert" not in shown


def test_a_word_result_is_judged_by_its_keywords(sample):
    assert row_for(sample, "Positive", {"low": None, "high": None, "range": "Negative"})[0]["status"] == "abnormal"
    assert row_for(sample, "Not detected", {"low": None, "high": None, "range": "Negative"})[0]["status"] == "normal"


def test_the_lab_flag_decides_when_nothing_else_can(sample):
    row, shown = row_for(sample, "see attached", {"low": None, "high": None, "range": None}, lab_flag="H")
    assert row["status"] == "high" and shown["lab_flag"] == "High"


def test_values_pass_through_exactly_as_the_backend_sent_them(sample):
    raw = sample("batch_critical")
    row = raw["tests"][0]["results"][0]
    row["value"], row["reference"]["low"], row["reference"]["high"] = 177, 13, 43
    out = build_ai_input(raw).ai_json["tests"][0]["results"][0]
    assert out["value"] == 177 and out["reference"]["low"] == 13 and out["reference"]["high"] == 43
    assert build_ai_input(raw).rows[0]["status"] == "high"


def test_a_not_set_text_range_does_not_become_bounds(sample):
    for marker in ("-1 - 9999", "0 - 0"):
        row, shown = row_for(sample, "5", {"low": None, "high": None, "range": marker})
        assert "low" not in shown["reference"] and "high" not in shown["reference"]
        assert row["status"] == "text"
    assert row_for(sample, "5", {"low": None, "high": None, "range": "1 - 4"})[0]["status"] == "high"


def test_descriptive_results_get_status_text_in_the_rows_but_not_in_the_ai_input(sample):
    processed = build_ai_input(sample("descriptive_only"))
    assert [r["status"] for r in processed.descriptive_rows] == ["text"]
    assert processed.descriptive_rows[0]["test_id"] == sample("descriptive_only")["descriptive_results"][0]["test_id"]
    assert "status" not in processed.ai_json["descriptive_results"][0]
    assert build_ai_input(sample("batch_normal")).descriptive_rows == []


def test_history_keeps_its_date_and_value_but_not_the_earlier_visit_id(sample):
    out = build_ai_input(sample("batch_critical")).ai_json["tests"][3]["results"][0]
    assert out["history"] and set(out["history"][0]) == {"history_date", "history_value"}


def test_performed_test_names_include_numeric_and_descriptive_tests(sample):
    names = build_ai_input(sample("descriptive_only")).performed_test_names
    assert "B -Thalassemia genetic defects by PCR" in names
    assert "Calcium" in build_ai_input(sample("batch_critical")).performed_test_names


def test_descriptive_text_and_its_history_are_plain_text_for_the_ai_and_the_database_row(sample):
    raw = sample("descriptive_only")
    raw["descriptive_results"][0]["text"] = "<p>Negative for <b>mutations</b></p><p>No variant &amp; no deletion</p>"
    raw["descriptive_results"][0]["history"] = [
        {"history_visit_id": 5, "history_date": "2026-01-10", "history_value": "<i>Negative</i>"}]
    processed = build_ai_input(raw)
    assert processed.ai_json["descriptive_results"][0]["text"] == "Negative for mutations\nNo variant & no deletion"
    assert processed.ai_json["descriptive_results"][0]["history"][0]["history_value"] == "Negative"
    assert processed.descriptive_rows[0]["text_value"] == "Negative for mutations\nNo variant & no deletion"
    assert "<" not in str(processed.ai_json)
