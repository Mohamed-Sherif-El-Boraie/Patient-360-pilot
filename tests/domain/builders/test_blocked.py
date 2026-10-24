import json

import pytest

from patient360.domain.builders.blocked import BLOCKED_MESSAGES, blocked_ai, blocked_reason, is_blocked

CANARY = "CANARY-MATCHED-TEXT"


def intervened(**trace):
    """A record shaped like the phase 4 blocked file: the action key and a guardrail trace."""
    return {"amazon-bedrock-guardrailAction": "INTERVENED", "amazon-bedrock-trace": {"guardrail": trace}}


def output(policy):
    return {"output": [{"guardrail-id": policy}]}


TOPIC = {"topicPolicy": {"topics": [{"name": "Medication dosing", "type": "DENY", "action": "BLOCKED"}]}}
WORD = {"wordPolicy": {"customWords": [{"match": CANARY, "action": "BLOCKED"}]}}
PII = {"sensitiveInformationPolicy": {"piiEntities": [{"type": "NAME", "match": CANARY, "action": "BLOCKED"}]}}
GROUNDING = {"contextualGroundingPolicy": {"filters": [{"type": "GROUNDING", "score": 0.1, "action": "BLOCKED"}]}}


def test_only_an_intervened_record_is_blocked():
    assert is_blocked(intervened())
    assert not is_blocked({"amazon-bedrock-guardrailAction": "NONE"}) and not is_blocked({})
    assert is_blocked({"metadata": {"en": {"amazon-bedrock-guardrailAction": "INTERVENED"}}})


@pytest.mark.parametrize(("record", "reason"), [
    (intervened(**output(TOPIC)), "default"),
    (intervened(**output(WORD)), "default"),
    (intervened(**output(PII)), "default"),                                       # no special message for personal data
    (intervened(**output(GROUNDING)), "ungrounded"),
    (intervened(input={"guardrail-id": TOPIC}), "input"),
    (intervened(input={"guardrail-id": PII}, **output(GROUNDING)), "input"),     # an input block wins
    (intervened(), "default"),                                                     # no usable trace: still a message
    ({"amazon-bedrock-guardrailAction": "INTERVENED"}, "default"),
])
def test_the_reason_comes_from_the_kind_of_policy_that_blocked(record, reason):
    assert blocked_reason(record) == reason


def test_a_policy_that_only_assessed_without_blocking_is_not_a_reason():
    detected = {"sensitiveInformationPolicy": {"piiEntities": [{"type": "NAME", "action": "ANONYMIZED"}]}}
    assert blocked_reason(intervened(**output({**detected, **TOPIC}))) == "default"


@pytest.mark.parametrize("record", [
    intervened(**output(PII)), intervened(input={"id": WORD}), intervened(**output(WORD))])
def test_nothing_from_the_trace_is_copied_into_the_message(record):
    ai = blocked_ai(record)
    assert CANARY not in json.dumps(ai, ensure_ascii=False) and "Medication" not in json.dumps(ai)
    assert ai["summary"] == BLOCKED_MESSAGES[blocked_reason(record)]
    assert [ai[k] for k in ("simple_summary", "critical_alerts", "note", "recommendations")] == [None] * 4


def test_every_message_has_english_and_arabic():
    assert all(set(m) == {"en", "ar"} and all(m.values()) for m in BLOCKED_MESSAGES.values())
