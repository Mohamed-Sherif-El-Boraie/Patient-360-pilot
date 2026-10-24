import json

import pytest

from patient360.domain.builders.blocked import BLOCKED_MESSAGES, blocked_ai, blocked_reason, is_blocked

CANARY = "CANARY-MATCHED-TEXT"


def intervened(**trace):
    """A record shaped like the phase 4 blocked file: the action key and a guardrail trace."""
    return {"amazon-bedrock-guardrailAction": "INTERVENED", "amazon-bedrock-trace": {"guardrail": trace}}


TOPIC = {"topicPolicy": {"topics": [{"name": "Medication dosing", "type": "DENY", "action": "BLOCKED"}]}}
WORD = {"wordPolicy": {"customWords": [{"match": CANARY, "action": "BLOCKED"}]}}


def test_only_an_intervened_record_is_blocked():
    assert is_blocked(intervened())
    assert not is_blocked({"amazon-bedrock-guardrailAction": "NONE"}) and not is_blocked({})
    assert is_blocked({"metadata": {"en": {"amazon-bedrock-guardrailAction": "INTERVENED"}}})


@pytest.mark.parametrize(("record", "reason"), [
    (intervened(input={"guardrail-id": TOPIC}), "input"),
    (intervened(inputAssessment={"guardrail-id": WORD}), "input"),                 # the other name AWS uses
    (intervened(output=[{"guardrail-id": TOPIC}]), "output"),
    (intervened(outputs=[{"guardrail-id": WORD}]), "output"),
    (intervened(input={"id": TOPIC}, output=[{"id": WORD}]), "input"),            # an input block wins
    (intervened(), "output"),                                                       # no usable trace: still a message
    ({"amazon-bedrock-guardrailAction": "INTERVENED"}, "output"),
])
def test_the_message_depends_only_on_whether_the_input_or_the_output_was_blocked(record, reason):
    assert blocked_reason(record) == reason


def test_an_input_that_was_only_assessed_without_blocking_does_not_count_as_blocked_input():
    assessed = {"sensitiveInformationPolicy": {"piiEntities": [{"type": "NAME", "action": "ANONYMIZED"}]}}
    assert blocked_reason(intervened(input={"id": assessed}, output=[{"id": TOPIC}])) == "output"


@pytest.mark.parametrize("record", [intervened(input={"id": WORD}), intervened(output=[{"id": WORD}]),
                                    intervened(output=[{"id": TOPIC}])])
def test_nothing_from_the_trace_is_copied_into_the_message(record):
    ai = blocked_ai(record)
    assert CANARY not in json.dumps(ai, ensure_ascii=False) and "Medication" not in json.dumps(ai)
    assert ai["summary"] == BLOCKED_MESSAGES[blocked_reason(record)]
    assert [ai[k] for k in ("simple_summary", "critical_alerts", "note", "recommendations")] == [None] * 4


def test_there_are_two_messages_each_in_english_and_arabic():
    assert set(BLOCKED_MESSAGES) == {"input", "output"}
    assert all(set(m) == {"en", "ar"} and all(m.values()) for m in BLOCKED_MESSAGES.values())
