import copy

import pytest

from patient360.domain.document import build_visit_document

CASES = ["01-batch-normal", "02-batch-critical", "03-urgent", "04-on-demand", "06-descriptive"]
FORBIDDEN = {"patient_id", "visit_id", "company_name", "lab_remark", "symptoms", "trend", "value_raw", "catalogue"}


def _keys(node, found=None):
    found = set() if found is None else found
    if isinstance(node, dict):
        for key, child in node.items():
            found.add(key)
            _keys(child, found)
    elif isinstance(node, list):
        for child in node:
            _keys(child, found)
    return found


@pytest.mark.parametrize("case", CASES)
def test_document_equals_the_flow_2a_example(flow, case):
    raw = flow(f"{case}/1-backend-input.json")
    expected = flow(f"{case}/2a-ai-service-document-before-rag.json")
    assert build_visit_document(raw).document == expected


@pytest.mark.parametrize("case", CASES)
def test_document_has_no_ids_company_or_retired_keys(flow, case):
    doc = build_visit_document(flow(f"{case}/1-backend-input.json")).document
    assert not (_keys(doc) & FORBIDDEN)


def test_input_is_never_mutated(flow):
    raw = flow("02-batch-critical/1-backend-input.json")
    before = copy.deepcopy(raw)
    build_visit_document(raw)
    assert raw == before


def test_critical_patient_rows(flow):
    doc = build_visit_document(flow("02-batch-critical/1-backend-input.json")).document
    by_name = {r["parameter_name"]: r for t in doc["tests"] for r in t["results"]}
    calcium = by_name["Calcium"]
    assert (calcium["status"], calcium["alert"], calcium["lab_flag"]) == ("low", "CRITICAL", "Low")
    assert calcium["critical"]["direction"] == "critical_low" and calcium["value"] == "5.3"
    assert by_name["Blood Urea"]["value"] == "177" and by_name["Blood Urea"]["alert"] == "MEDICAL_ATTENTION"
    assert by_name["CREATININE (serum)"]["critical"]["direction"] == "critical_high"
    assert by_name["Glucose After 2 Hours"]["history"] == [
        {"date": "2026-01-10", "value": "241"}, {"date": "2026-05-02", "value": "205.5"}]


def test_normal_patient_has_a_critical_limit_but_no_direction(flow):
    doc = build_visit_document(flow("01-batch-normal/1-backend-input.json")).document
    creatinine = next(r for t in doc["tests"] for r in t["results"] if r["parameter_name"] == "CREATININE (serum)")
    assert creatinine["alert"] == "NORMAL" and "direction" not in creatinine["critical"]


def test_values_pass_through_exactly_as_the_backend_sent_them(flow):
    raw = flow("02-batch-critical/1-backend-input.json")
    doc = build_visit_document(raw).document
    sent = [r for t in raw["tests"] for r in t["results"]]
    got = [r for t in doc["tests"] for r in t["results"]]
    for before, after in zip(sent, got, strict=True):
        assert after["value"] == before["value"] and type(after["value"]) is type(before["value"])
        assert after.get("previous_value") == before.get("previous_value")


def test_numbers_from_the_backend_stay_numbers_and_still_classify(flow):
    raw = copy.deepcopy(flow("02-batch-critical/1-backend-input.json"))
    row = raw["tests"][0]["results"][0]
    row["value"], row["previous_value"] = 177, 200
    row["reference"]["low"], row["reference"]["high"] = 13, 43
    out = build_visit_document(raw).document["tests"][0]["results"][0]
    assert out["value"] == 177 and out["previous_value"] == 200
    assert out["reference"]["low"] == 13 and out["reference"]["high"] == 43
    assert out["status"] == "high"
