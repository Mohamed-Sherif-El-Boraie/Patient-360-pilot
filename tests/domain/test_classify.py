import pytest

from patient360.domain.classify import LANE_BY_TIER, classify_patient, lane_for
from patient360.domain.document import apply_logic

EXPECTED = {
    "01-batch-normal": "NORMAL", "02-batch-critical": "CRITICAL", "03-urgent": "MILD",
    "04-on-demand": "CRITICAL", "06-descriptive": "MODERATE",
}


@pytest.mark.parametrize("case,tier", EXPECTED.items())
def test_tier_matches_the_flow_output(flow, case, tier):
    flat = apply_logic(flow(f"{case}/1-backend-input.json")).flat
    assert classify_patient(flat) == tier
    assert flow(f"{case}/5-ai-service-output.json")["tier"] == tier


def row(status, *, critical=False, value="1"):
    return {"value": value, "status": status, "is_critical": critical}


def test_precedence_critical_then_unrangeable_then_three_abnormal_then_mild_then_normal():
    assert classify_patient([row("high", critical=True), row(None)]) == "CRITICAL"
    assert classify_patient([row(None), row("normal")]) == "MODERATE"                    # unrangeable: fail safe
    assert classify_patient([row("high"), row("low"), row("abnormal")]) == "MODERATE"    # 3+ abnormal
    assert classify_patient([row("high"), row("low")]) == "MILD"
    assert classify_patient([row("normal"), row("normal")]) == "NORMAL"


def test_rows_without_a_result_are_ignored_and_no_data_escalates():
    assert classify_patient([row(None, value=""), row("normal")]) == "NORMAL"
    assert classify_patient([]) == "MODERATE"


def test_lanes():
    tiers = ("NORMAL", "MILD", "MODERATE", "CRITICAL")
    assert [lane_for(t) for t in tiers] == ["normal", "normal", "critical", "critical"]
    assert set(LANE_BY_TIER) == {"NORMAL", "MILD", "MODERATE", "CRITICAL"}
