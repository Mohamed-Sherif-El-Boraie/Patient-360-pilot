import pytest

from patient360.domain.ai_input import build_ai_input
from patient360.domain.logic import critical_bounds, reference_bounds


@pytest.mark.parametrize("low,high,expected", [
    ("0", "0", (None, None)),
    ("-1", "9999", (None, None)),
    ("-1", "99999999", (None, None)),
    ("-1", "6", (-1, 6)),           # a lone -1 is a real low limit
    ("0", "5", (0, 5)),
    ("13", "9999", (13, 9999)),     # a lone 9999 is a real high limit
    (None, None, (None, None)),
])
def test_reference_not_set_markers_count_only_as_a_pair(low, high, expected):
    assert reference_bounds(low, high) == expected


@pytest.mark.parametrize("low,high,expected", [
    ("-1", "9999", (None, None)),
    ("-9999", "99999", (None, None)),
    ("-99999", "999999", (None, None)),
    ("-1", "6", (-1, 6)),           # -1 next to a real high is a real limit
    ("5", "9999", (5, 9999)),       # 9999 next to a real low is a real limit
    ("-10", "6", (-10, 6)),         # a negative critical low that is not a marker stays valid
    ("-5", "9999", (-5, 9999)),
    (None, None, (None, None)),
])
def test_critical_not_set_markers_count_only_as_a_pair(low, high, expected):
    assert critical_bounds(low, high) == expected


def result(sample, value, reference, critical):
    raw = sample("urgent_mild")
    raw["tests"] = [{"test_id": 1, "test_name": "T", "test_remark": None, "results": [{
        "parameter_id": 1, "parameter_name": "P", "result_date": "2026-10-20", "value": value, "unit": None,
        "reference": reference, "lab_flag": None, "critical": critical, "parameter_remark": None, "history": []}]}]
    processed = build_ai_input(raw)
    return processed.rows[0], processed.ai_json["tests"][0]["results"][0]


def test_a_real_critical_limit_is_shown_exactly_as_sent(sample):
    _, shown = result(sample, "5", {"low": "1", "high": "9"}, {"low": -1, "high": 6, "range_text": None})
    assert shown["critical"]["low"] == -1 and shown["critical"]["high"] == 6


def test_a_marker_pair_is_left_out_of_the_ai_input(sample):
    _, shown = result(sample, "5", {"low": "1", "high": "9"}, {"low": -1, "high": 9999, "range_text": None})
    assert "critical" not in shown or ("low" not in shown["critical"] and "high" not in shown["critical"])


def test_a_negative_critical_low_is_a_real_limit_base_excess(sample):
    row, shown = result(sample, "-12", {"low": "-2", "high": "2"}, {"low": -10, "high": 9999, "range_text": None})
    assert row["status"] == "critical" and row["critical_direction"] == "critical_low" and shown["alert"] == "CRITICAL"
