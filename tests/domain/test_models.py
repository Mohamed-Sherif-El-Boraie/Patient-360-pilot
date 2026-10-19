import copy
import json

import pytest

from patient360.domain.validate import Invalid, Valid, validate_payload

VISIT_CASES = ["01-batch-normal", "02-batch-critical", "03-urgent", "04-on-demand", "06-descriptive"]


def raw(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


@pytest.mark.parametrize("case", VISIT_CASES)
def test_every_flow_visit_input_is_valid(flow, case):
    assert isinstance(validate_payload(raw(flow(f"{case}/1-backend-input.json"))), Valid)


@pytest.mark.parametrize("name", ["05-symptom-checker/1-backend-input.json",
                                  "05b-symptom-checker-guardrail-blocked/1-backend-input.json"])
def test_symptom_checker_inputs_are_valid(flow, name):
    assert isinstance(validate_payload(raw(flow(name))), Valid)


def test_unknown_key_is_rejected(flow):
    body = flow("03-urgent/1-backend-input.json")
    body["status"] = "high"                      # logic keys must never come from the Backend
    result = validate_payload(raw(body))
    assert isinstance(result, Invalid) and result.errors[0]["loc"] == ["status"]


def test_the_source_key_picks_the_model(flow):
    visit = flow("03-urgent/1-backend-input.json")
    visit["source"] = "symptom-checker"         # a visit body claiming to be a symptom check has the wrong keys
    assert isinstance(validate_payload(raw(visit)), Invalid)


@pytest.mark.parametrize("source", ["nope", "", None, 7, ["urgent"]])
def test_a_missing_or_unknown_source_is_invalid(flow, source):
    body = flow("03-urgent/1-backend-input.json")
    body["source"] = source
    assert validate_payload(raw(body)).errors[0]["type"] == "unknown_source"


def test_a_json_list_or_scalar_is_invalid_not_a_crash():
    assert validate_payload(b"[1, 2]").errors[0]["type"] == "unknown_source"
    assert validate_payload(b"42").errors[0]["type"] == "unknown_source"


def test_strict_types_reject_a_string_visit_id(flow):
    body = flow("03-urgent/1-backend-input.json")
    body["visit_id"] = "2070000003"
    assert isinstance(validate_payload(raw(body)), Invalid)


def test_symptom_text_is_limited_to_500_characters(flow):
    body = copy.deepcopy(flow("05-symptom-checker/1-backend-input.json"))
    body["patient_symptoms"] = "x" * 501
    assert isinstance(validate_payload(raw(body)), Invalid)
    body["patient_symptoms"] = "x" * 500
    assert isinstance(validate_payload(raw(body)), Valid)


def test_very_deep_nesting_is_invalid_not_a_crash():
    assert validate_payload(b"[" * 200000).errors[0]["type"] == "json_invalid"


def test_unknown_key_names_are_cut_short_in_the_error(flow):
    body = flow("03-urgent/1-backend-input.json")
    body["k" * 500] = 1
    loc = validate_payload(raw(body)).errors[0]["loc"]
    assert len(loc[0]) == 60


def test_broken_json():
    assert validate_payload(b'{"a": ').errors[0]["type"] == "json_invalid"


def test_errors_never_echo_the_input(flow):
    body = flow("05-symptom-checker/1-backend-input.json")
    body["patient_symptoms"] = "SECRET-SYMPTOM-TEXT " * 100   # too long -> an error that could echo the value
    result = validate_payload(raw(body))
    assert isinstance(result, Invalid) and "SECRET-SYMPTOM-TEXT" not in json.dumps(result.errors)


def test_values_and_limits_may_arrive_as_text_decimal_or_integer(flow):
    body = copy.deepcopy(flow("02-batch-critical/1-backend-input.json"))
    row = body["tests"][0]["results"][0]
    row["value"], row["previous_value"] = 177, 200.5
    row["reference"]["low"], row["reference"]["high"] = 13, 43.5
    row["history"] = [{"date": "2026-01-10", "value": 241}]
    assert isinstance(validate_payload(raw(body)), Valid)


def test_a_boolean_is_not_a_value(flow):
    body = copy.deepcopy(flow("02-batch-critical/1-backend-input.json"))
    body["tests"][0]["results"][0]["value"] = True
    assert isinstance(validate_payload(raw(body)), Invalid)
