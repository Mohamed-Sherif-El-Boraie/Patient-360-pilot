import pytest

from patient360.domain.builders.ai_input import build_ai_input
from patient360.domain.rules.classify import LANE_BY_TIER, decide_tier, lane_for_tier

EXPECTED = {"batch_normal": "NORMAL", "batch_critical": "CRITICAL", "urgent_mild": "MILD",
            "on_demand_critical": "CRITICAL", "descriptive_only": "MODERATE"}


@pytest.mark.parametrize("name,tier", EXPECTED.items())
def test_tier_of_each_sample_visit(sample, name, tier):
    assert decide_tier(build_ai_input(sample(name)).rows) == tier


def row(status, value="1"):
    return {"value": value, "status": status}


def test_all_normal_is_normal():
    assert decide_tier([row("normal"), row("normal")]) == "NORMAL"


@pytest.mark.parametrize("status", ["high", "low", "abnormal", "text"])
def test_each_not_normal_status_counts_toward_mild(status):
    assert decide_tier([row("normal"), row(status)]) == "MILD"


def test_one_to_three_not_normal_is_mild_and_four_is_moderate():
    assert decide_tier([row("high"), row("low"), row("text")]) == "MILD"
    assert decide_tier([row("high"), row("low"), row("text"), row("abnormal")]) == "MODERATE"


def test_one_critical_parameter_makes_the_visit_critical_whatever_the_rest():
    assert decide_tier([row("normal"), row("critical")]) == "CRITICAL"
    assert decide_tier([row("high")] * 9 + [row("critical")]) == "CRITICAL"


def test_rows_without_a_result_are_ignored_and_no_data_escalates():
    assert decide_tier([row("text", value=""), row("normal")]) == "NORMAL"
    assert decide_tier([]) == "MODERATE"


def test_each_descriptive_result_counts_as_one_text():
    assert decide_tier([row("normal")], descriptive_count=1) == "MILD"
    assert decide_tier([row("normal")], descriptive_count=3) == "MILD"
    assert decide_tier([row("normal")], descriptive_count=4) == "MODERATE"
    assert decide_tier([row("critical")], descriptive_count=2) == "CRITICAL"


def test_only_descriptive_results_is_moderate():
    assert decide_tier([], descriptive_count=1) == "MODERATE"


def test_lanes_follow_the_tier():
    tiers = ("NORMAL", "MILD", "MODERATE", "CRITICAL")
    assert [lane_for_tier(t) for t in tiers] == ["normal", "normal", "critical", "critical"]
    assert set(LANE_BY_TIER) == set(tiers)
