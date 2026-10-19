import pytest

from patient360.domain.document import apply_logic
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


def row(value, reference, critical):
    raw = {"source": "urgent", "patient": {"age_years": 30, "sex": "male"}, "tests": [{"test_name": "T", "results": [
        {"parameter_name": "P", "value": value, "reference": reference, "critical": critical}]}]}
    return apply_logic(raw).doc["tests"][0]["results"][0]


def test_a_real_critical_limit_is_shown_exactly_as_sent():
    shown = row("5", {"low": "1", "high": "9"}, {"low": -1, "high": 6})["critical"]
    assert shown["low"] == -1 and shown["high"] == 6


def test_a_marker_pair_is_left_out_of_the_document():
    result = row("5", {"low": "1", "high": "9"}, {"low": -1, "high": 9999})
    assert "critical" not in result or ("low" not in result["critical"] and "high" not in result["critical"])


def test_a_negative_critical_low_is_a_real_limit_base_excess():
    result = row("-12", {"low": "-2", "high": "2"}, {"low": -10, "high": 9999})
    assert result["status"] == "low" and result["critical"]["direction"] == "critical_low"
    assert result["alert"] == "CRITICAL"
