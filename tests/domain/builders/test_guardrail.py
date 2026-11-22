"""What the guardrail of an AWS answer becomes: check rows and finding rows (pure, no database)."""
from patient360.domain.builders.guardrail import guardrail_checks

# the real batch shape (every line of the owner's sample files): action NONE, bypassed, nothing assessed
BATCH_NONE = {"action": "NONE", "bypassed": True, "outputs": [], "assessments": [],
              "usage": {"topicPolicyUnits": 0, "contentPolicyUnits": 0, "wordPolicyUnits": 0,
                        "sensitiveInformationPolicyUnits": 0, "sensitiveInformationPolicyFreeUnits": 0,
                        "contextualGroundingPolicyUnits": 0}}

# documented ApplyGuardrail shape (no real intervened sample yet)
ASSESSMENT = {
    "topicPolicy": {"topics": [{"name": "Medication dosing", "type": "DENY", "action": "BLOCKED"}]},
    "contentPolicy": {"filters": [{"type": "HATE", "confidence": "HIGH", "filterStrength": "HIGH",
                                   "action": "BLOCKED"}]},
    "wordPolicy": {"customWords": [{"match": "x", "action": "BLOCKED"}],
                   "managedWordLists": [{"match": "y", "type": "PROFANITY", "action": "BLOCKED"}]},
    "sensitiveInformationPolicy": {"piiEntities": [{"match": "a@b.c", "type": "EMAIL", "action": "ANONYMIZED"}],
                                   "regexes": [{"name": "mrn", "match": "123", "regex": "\\d+", "action": "BLOCKED"}]},
    "contextualGroundingPolicy": {"filters": [{"type": "GROUNDING", "threshold": 0.8, "score": 0.2,
                                               "action": "BLOCKED"}]},
    "invocationMetrics": {"guardrailProcessingLatency": 123,
                          "usage": {"topicPolicyUnits": 1, "contentPolicyUnits": 1, "wordPolicyUnits": 1,
                                    "sensitiveInformationPolicyUnits": 1, "sensitiveInformationPolicyFreeUnits": 0,
                                    "contextualGroundingPolicyUnits": 1},
                          "guardrailCoverage": {"textCharacters": {"guarded": 40, "total": 50}}},
}


def test_no_guardrail_object_means_no_check():
    assert guardrail_checks({"recordId": "k"}) == []
    assert guardrail_checks({"guardrail": "odd"}) == []
    assert guardrail_checks({"guardrail": None}) == []


def test_the_real_batch_none_sample_is_one_output_check_with_zero_units_and_no_findings():
    [check] = guardrail_checks({"guardrail": BATCH_NONE})
    assert (check["direction"], check["guardrail_id"], check["action"], check["bypassed"]) == (
        "output", "unknown", "NONE", True)
    assert check["topic_policy_units"] == 0 and check["contextual_grounding_policy_units"] == 0
    assert check["latency_ms"] is None and check["findings"] == []


def test_a_batch_intervened_assessment_gives_every_policy_finding_and_the_units_and_latency():
    guardrail = {"action": "GUARDRAIL_INTERVENED", "bypassed": False, "usage": BATCH_NONE["usage"] | {
        "topicPolicyUnits": 2}, "assessments": [ASSESSMENT]}
    [check] = guardrail_checks({"guardrail": guardrail})
    assert (check["action"], check["bypassed"], check["topic_policy_units"]) == ("INTERVENED", False, 2)
    assert (check["latency_ms"], check["text_chars_guarded"], check["text_chars_total"]) == (123, 40, 50)
    found = {(f["policy"], f["name"], f["type"], f["confidence"], f["filter_strength"], f["action"])
             for f in check["findings"]}
    assert found == {
        ("topic", "Medication dosing", "DENY", None, None, "BLOCKED"),
        ("content", None, "HATE", "HIGH", "HIGH", "BLOCKED"),
        ("word", "x", "CUSTOM", None, None, "BLOCKED"),
        ("word", "y", "PROFANITY", None, None, "BLOCKED"),
        ("sensitive_info", None, "EMAIL", None, None, "ANONYMIZED"),
        ("sensitive_info", "mrn", "REGEX", None, None, "BLOCKED"),
        ("contextual_grounding", None, "GROUNDING", None, None, "BLOCKED")}
    assert check["raw"] == [ASSESSMENT]


def test_an_invoke_model_trace_gives_an_input_and_an_output_check_per_guardrail_id():
    record = {"amazon-bedrock-guardrailAction": "INTERVENED",
              "amazon-bedrock-trace": {"guardrail": {"input": {"gr1": {"topicPolicy": {"topics": [
                  {"name": "T", "type": "DENY", "action": "BLOCKED"}]}}}, "outputs": [{"gr1": {}}]}}}
    checks = {c["direction"]: c for c in guardrail_checks(record)}
    assert set(checks) == {"input", "output"}
    assert checks["input"]["guardrail_id"] == "gr1" and checks["input"]["action"] == "INTERVENED"
    assert [f["name"] for f in checks["input"]["findings"]] == ["T"]
    assert checks["output"]["action"] == "NONE" and checks["output"]["findings"] == []


def test_an_invoke_model_none_with_an_empty_trace_is_still_one_check():
    record = {"amazon-bedrock-guardrailAction": "NONE",
              "amazon-bedrock-trace": {"guardrail": {"input": {}, "outputs": []}}}
    [check] = guardrail_checks(record)
    assert (check["direction"], check["guardrail_id"], check["action"]) == ("output", "unknown", "NONE")


def test_a_converse_intervention_reads_input_and_output_assessments():
    record = {"stopReason": "guardrail_intervened", "trace": {"guardrail": {
        "inputAssessment": {"gr2": {"contentPolicy": {"filters": [
            {"type": "VIOLENCE", "confidence": "LOW", "filterStrength": "MEDIUM", "action": "BLOCKED"}]}}}}}}
    [check] = guardrail_checks(record)
    assert (check["direction"], check["guardrail_id"], check["action"]) == ("input", "gr2", "INTERVENED")


def test_odd_shapes_never_raise():
    for odd in ({"guardrail": {"action": 5, "assessments": "x", "usage": []}},
                {"amazon-bedrock-guardrailAction": "NONE", "amazon-bedrock-trace": {"guardrail": []}},
                {"guardrail": {"assessments": [None, 3, {"topicPolicy": {"topics": [7, {"name": "ok"}]}}]}}):
        assert isinstance(guardrail_checks(odd), list)
