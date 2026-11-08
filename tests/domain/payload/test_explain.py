import copy
import json
from pathlib import Path

from patient360.domain.builders.ai_input import build_ai_input
from patient360.domain.payload.validate import Invalid, Valid, validate_payload
from patient360.http_app import invalid_message

TOOLS_SAMPLES = Path(__file__).parents[3] / "tools" / "samples"


def raw(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def messages(body) -> list[str]:
    result = validate_payload(raw(body))
    assert isinstance(result, Invalid)
    return list(dict.fromkeys(e["message"] for e in result.errors))


def test_a_wrong_value_names_the_test_and_the_parameter(sample):
    body = sample("urgent_mild")
    test, result = body["tests"][0], body["tests"][0]["results"][0]
    result["value"] = ["SECRET"]
    assert messages(body) == [f"tests, test_id {test['test_id']}, parameter_id {result['parameter_id']}: "
                              "value must be text or a number"]
    assert "SECRET" not in invalid_message(validate_payload(raw(body)).errors)


def test_a_bad_date_a_bad_flag_and_an_unknown_field_are_told_in_words(sample):
    body = sample("urgent_mild")
    test, result = body["tests"][0], body["tests"][0]["results"][0]
    where = f"tests, test_id {test['test_id']}, parameter_id {result['parameter_id']}"
    result["result_date"], result["lab_flag"], result["xyz"] = "31/10/2026", "HH", 1
    assert messages(body) == [f"{where}: result_date must be yyyy-MM-dd", f"{where}: lab_flag must be H or L",
                              f'{where}: unknown field "xyz"']


def test_a_missing_list_says_to_send_an_empty_one(sample):
    body = sample("urgent_mild")
    del body["visit_answer_list"]
    assert messages(body) == ["visit_answer_list is required (send [] if there are none)"]


def test_a_descriptive_result_is_found_by_its_test_id(sample):
    body = sample("descriptive_only")
    item = body["descriptive_results"][0]
    del item["text"]
    assert messages(body) == [f"descriptive_results, test_id {item['test_id']}: text is required"]


def test_a_test_sent_twice_names_its_id(sample):
    body = sample("urgent_mild")
    body["tests"].append(copy.deepcopy(body["tests"][0]))
    assert messages(body) == [f"test_id {body['tests'][0]['test_id']} appears twice"]


def test_a_parameter_sent_twice_names_the_test_and_the_parameter(sample):
    body = sample("urgent_mild")
    test = body["tests"][0]
    test["results"].append(copy.deepcopy(test["results"][0]))
    assert messages(body) == [f"tests, test_id {test['test_id']}: parameter_id {test['results'][0]['parameter_id']}"
                              " appears twice"]


def test_a_test_without_a_usable_id_is_found_by_its_position(sample):
    body = sample("urgent_mild")
    body["tests"][0]["test_id"] = "SECRET-ID"
    assert messages(body) == ["tests, tests #1: test_id must be a whole number"]


def test_the_answer_tells_each_problem_once_and_keeps_its_limits(sample):
    body = sample("urgent_mild")
    body["tests"][0]["results"][0]["value"] = {"a": 1}      # a ``str | int | float`` value fails three times
    message = invalid_message(validate_payload(raw(body)).errors)
    assert message.count("value must be text or a number") == 1 and message.startswith("Invalid payload: tests, ")
    assert len(message) <= 300


def test_every_tools_sample_is_a_valid_visit():
    files = sorted(TOOLS_SAMPLES.glob("*_source.json")) + sorted(TOOLS_SAMPLES.glob("batch_visit_*.json"))
    assert files
    for path in files:
        if "symptom" not in path.name:
            assert isinstance(validate_payload(path.read_bytes()), Valid), path.name


def test_a_descriptive_result_with_no_text_is_accepted_and_not_sent_to_the_ai(sample):
    body = sample("descriptive_only")
    empty = copy.deepcopy(body["descriptive_results"][0])
    empty.update(test_id=empty["test_id"] + 1, test_name="Ophthalmology Examination", text="")
    blank_html = copy.deepcopy(empty) | {"test_id": empty["test_id"] + 1, "text": "<p>&nbsp;</p>"}
    body["descriptive_results"] += [empty, blank_html]
    assert isinstance(validate_payload(raw(body)), Valid)
    processed = build_ai_input(body)
    assert [r["test_id"] for r in processed.descriptive_rows] == [body["descriptive_results"][0]["test_id"]]
    assert len(processed.ai_json["descriptive_results"]) == 1
    assert "Ophthalmology Examination" in processed.performed_test_names      # done, only without a report
