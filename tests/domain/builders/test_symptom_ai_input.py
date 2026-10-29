import copy
import json

from patient360.domain.builders.ai_input import build_ai_input
from patient360.domain.builders.symptom_ai_input import build_symptom_ai_input, keep_english_only

STORED_AI = {
    "summary": {"en": "Kidney results are abnormal.", "ar": "نتائج الكلى غير طبيعية"},
    "simple_summary": {"en": "Your kidneys need care.", "ar": "كليتاك"},
    "critical_alerts": [{"en": "Creatinine is critical", "ar": "الكرياتينين"}],
    "note": {"en": "These tests help.", "ar": "تساعد"},
    "recommendations": [{
        "test_name": "Estimated (eGFR)", "priority": {"en": "urgent", "ar": "عاجل"},
        "reason": {"en": "kidney filter", "ar": "x"}, "timing": {"en": "today", "ar": "y"},
        "guideline": {"en": "KDIGO", "ar": "z"},
        "advisory": {"level": {"en": "seek_care_promptly", "ar": "q"}, "message": {"en": "see a doctor", "ar": "w"}}}],
    "packages": [{"package_name": "Kidney check", "tests": [{"test_name": "Urea"}]}],
}


def build(sample):
    return (sample("symptom_check"), build_ai_input(sample("batch_critical")).ai_json, STORED_AI)


def test_the_ai_input_holds_symptoms_current_results_history_and_the_previous_analysis(sample):
    doc = build_symptom_ai_input(*build(sample))
    assert doc["source"] == "symptom-checker" and doc["patient_symptoms"] == "headache and tiredness for a week"
    assert doc["patient"]["sex"] and doc["visit_answer_list"]
    assert any(r.get("history") for t in doc["tests"] for r in t["results"])
    expected = {"summary", "simple_summary", "critical_alerts", "note", "recommendations"}
    assert set(doc["previous_ai_analysis"]) == expected


def test_the_time_the_symptoms_were_received_reaches_the_model_when_given(sample):
    given = build_symptom_ai_input(*build(sample), "2026-10-29T08:30:00Z")
    assert given["symptom_received_at"] == "2026-10-29T08:30:00Z"
    assert "symptom_received_at" not in build_symptom_ai_input(*build(sample))


def test_the_previous_analysis_is_english_only_and_has_no_packages(sample):
    doc = build_symptom_ai_input(*build(sample))
    assert '"ar"' not in json.dumps(doc, ensure_ascii=False) and "packages" not in doc["previous_ai_analysis"]
    first = doc["previous_ai_analysis"]["recommendations"][0]
    assert first["priority"] == "urgent"
    assert first["advisory"] == {"level": "seek_care_promptly", "message": "see a doctor"}


def test_inputs_are_not_mutated(sample):
    args = build(sample)
    before = copy.deepcopy(args)
    build_symptom_ai_input(*args)
    assert args == before


def test_english_only_collapses_bilingual_texts_and_keeps_other_keys():
    node = {"a": {"en": "x", "ar": "y"}, "b": [{"en": "p", "ar": "q"}],
            "c": {"id": 1, "t": {"en": "z", "ar": "w"}}, "n": 5}
    assert keep_english_only(node) == {"a": "x", "b": ["p"], "c": {"id": 1, "t": "z"}, "n": 5}
