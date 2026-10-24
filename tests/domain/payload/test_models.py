import copy
import json

import pytest

from patient360.domain.payload.validate import Invalid, Valid, validate_payload

VISITS = ["batch_normal", "batch_critical", "urgent_mild", "on_demand_critical", "descriptive_only"]


def raw(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


@pytest.mark.parametrize("name", [*VISITS, "symptom_check"])
def test_every_sample_is_valid(sample, name):
    assert isinstance(validate_payload(raw(sample(name))), Valid)


def test_unknown_key_is_rejected(sample):
    body = sample("urgent_mild")
    body["status"] = "high"                      # logic keys must never come from the Backend
    result = validate_payload(raw(body))
    assert isinstance(result, Invalid) and result.errors[0]["loc"] == ["status"]


def test_the_source_number_picks_the_model(sample):
    visit = sample("urgent_mild")
    visit["source"] = 3                          # a visit body claiming to be a symptom check has the wrong keys
    assert isinstance(validate_payload(raw(visit)), Invalid)
    symptom = sample("symptom_check")
    symptom["source"] = 1
    assert isinstance(validate_payload(raw(symptom)), Invalid)


@pytest.mark.parametrize("source", [0, 5, "urgent", "1", None, True, 1.0, [1]])
def test_a_missing_or_unknown_source_is_invalid(sample, source):
    body = sample("urgent_mild")
    body["source"] = source
    assert validate_payload(raw(body)).errors[0]["type"] == "unknown_source"


def test_a_json_list_or_scalar_is_invalid_not_a_crash():
    assert validate_payload(b"[1, 2]").errors[0]["type"] == "unknown_source"
    assert validate_payload(b"42").errors[0]["type"] == "unknown_source"


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nan_and_infinity_are_not_json(sample, constant):
    text = json.dumps(sample("urgent_mild")).replace('"168"', constant, 1)
    assert validate_payload(text.encode()).errors[0]["type"] == "json_invalid"


@pytest.mark.parametrize("field", ["patient_id", "visit_id"])
@pytest.mark.parametrize("bad", [0, -5, 10**30])
def test_ids_must_fit_a_database_bigint(sample, field, bad):
    body = sample("urgent_mild")
    body[field] = bad
    assert isinstance(validate_payload(raw(body)), Invalid)


def test_test_and_parameter_ids_must_fit_a_database_int(sample):
    body = sample("urgent_mild")
    body["tests"][0]["test_id"] = 2**31
    assert isinstance(validate_payload(raw(body)), Invalid)


def test_the_same_parameter_twice_in_one_test_is_rejected(sample):
    body = sample("batch_critical")
    results = body["tests"][0]["results"]
    results.append(copy.deepcopy(results[0]))
    result = validate_payload(raw(body))
    assert isinstance(result, Invalid) and "twice" in result.errors[0]["msg"]


def test_the_same_test_twice_in_one_visit_is_rejected(sample):
    body = sample("batch_critical")
    body["tests"].append(copy.deepcopy(body["tests"][0]))
    assert isinstance(validate_payload(raw(body)), Invalid)


def test_the_same_parameter_in_two_different_tests_is_allowed(sample):
    body = sample("batch_critical")
    other = copy.deepcopy(body["tests"][0])
    other["test_id"] = 9999
    body["tests"].append(other)
    assert isinstance(validate_payload(raw(body)), Valid)


def test_strict_types_reject_a_string_visit_id(sample):
    body = sample("urgent_mild")
    body["visit_id"] = "2070000003"
    assert isinstance(validate_payload(raw(body)), Invalid)


def test_values_and_limits_may_arrive_as_text_decimal_or_integer(sample):
    body = sample("batch_critical")
    row = body["tests"][0]["results"][0]
    row["value"] = 177
    row["reference"]["low"], row["reference"]["high"] = 13, 43.5
    row["history"] = [{"history_visit_id": 5, "history_date": "2026-01-10", "history_value": 241}]
    row["critical"] = {"low": "-1", "high": 6, "range_text": None}
    assert isinstance(validate_payload(raw(body)), Valid)


def test_a_boolean_is_not_a_value(sample):
    body = sample("batch_critical")
    body["tests"][0]["results"][0]["value"] = True
    assert isinstance(validate_payload(raw(body)), Invalid)


def test_the_patient_has_a_birthdate_and_a_sex_number(sample):
    body = sample("urgent_mild")
    body["patient"]["sex"] = 3
    assert isinstance(validate_payload(raw(body)), Invalid)
    body["patient"] = {"age_years": 40, "sex": 1}
    assert isinstance(validate_payload(raw(body)), Invalid)


def test_symptom_text_is_limited_to_500_characters_and_cannot_be_blank(sample):
    body = sample("symptom_check")
    body["patient_symptoms"] = "x" * 501
    assert isinstance(validate_payload(raw(body)), Invalid)
    body["patient_symptoms"] = "x" * 500
    assert isinstance(validate_payload(raw(body)), Valid)
    body["patient_symptoms"] = "      "
    assert isinstance(validate_payload(raw(body)), Invalid)


def test_very_deep_nesting_is_invalid_not_a_crash():
    assert validate_payload(b"[" * 200000).errors[0]["type"] == "json_invalid"


def test_unknown_key_names_are_cut_short_in_the_error(sample):
    body = sample("urgent_mild")
    body["k" * 500] = 1
    assert len(validate_payload(raw(body)).errors[0]["loc"][0]) == 60


def test_broken_json():
    assert validate_payload(b'{"a": ').errors[0]["type"] == "json_invalid"


def test_errors_never_echo_the_input(sample):
    body = sample("symptom_check")
    body["patient_symptoms"] = "SECRET-SYMPTOM-TEXT " * 100   # too long -> an error that could echo the value
    result = validate_payload(raw(body))
    assert isinstance(result, Invalid) and "SECRET-SYMPTOM-TEXT" not in json.dumps(result.errors)


def test_a_birthdate_must_be_a_real_date(sample):
    body = sample("urgent_mild")
    body["patient"]["birthdate"] = "2026-13-45"
    assert isinstance(validate_payload(raw(body)), Invalid)
    body["patient"]["birthdate"] = "2026-02-29"
    assert isinstance(validate_payload(raw(body)), Invalid)
