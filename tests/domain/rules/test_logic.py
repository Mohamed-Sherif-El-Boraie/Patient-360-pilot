import pytest

from patient360.domain.rules.logic import (
    alert_level_for,
    classify_qualitative,
    classify_result,
    critical_breach,
    lab_flag_status,
    parse_text_range,
    reference_bounds,
)


@pytest.mark.parametrize("value,low,high,expected", [
    ("177", "13", "43", "high"),
    ("5.3", "8.6", "10.2", "low"),
    ("43", "13", "43", "normal"),      # exactly on a bound is normal
    ("13", "13", "43", "normal"),
    ("5", "0", "0", None),             # 0/0 means "no range recorded"
    ("500", "-1", "9999", None),       # -1/9999 means "no limit"
    ("5", None, "10", "normal"),       # one-sided range the value complies with
    ("12", None, "10", "high"),
    ("Positive", "1", "2", None),      # not a number
])
def test_classify_result(value, low, high, expected):
    assert classify_result(value, low, high) == expected


def test_reference_bounds_treats_lims_markers_as_no_range():
    assert reference_bounds("0", "0") == (None, None)
    assert reference_bounds("-1", "9999") == (None, None)
    assert reference_bounds("13", "43") == (13, 43)


@pytest.mark.parametrize("text,expected", [
    ("13 - 43", (13, 43)),
    ("6.7 to 22.4", (6.7, 22.4)),
    ("< 5.7", (None, 5.7)),
    (">= 2.0", (2, None)),
    ("up to 30", (None, 30)),
    ("43 - 13", (13, 43)),             # a reversed pair is a data-entry artefact
    ("Negative", (None, None)),
    ("1:64", (None, None)),            # a titre is not a range
    ("", (None, None)),
])
def test_parse_text_range(text, expected):
    assert parse_text_range(text) == expected


@pytest.mark.parametrize("value,expected_text,expected", [
    ("Negative", None, "normal"),
    ("Target not Detected", None, "normal"),
    ("Negative for all tested mutations and deletions", None, "normal"),
    ("Not Detected", None, "normal"),
    ("Non detected", None, "normal"),
    ("Positive", None, "abnormal"),
    ("Weak Positive", None, "abnormal"),
    ("Positive at Heterozygous state", None, "abnormal"),
    ("Nil", "Nil", "normal"),          # equals the expected text
    ("Nil", None, None),
    ("Yellow", None, None),
    ("Normal Trace", None, None),
    ("Negative and positive", None, None),   # conflicting wording stays unjudged
])
def test_classify_qualitative(value, expected_text, expected):
    assert classify_qualitative(value, expected_text) == expected


def test_not_reactive_is_negative():
    # v3 read it as positive because it contains "reactive"; the owner decided it is negative.
    assert classify_qualitative("Not reactive", None) == "normal"
    assert classify_qualitative("Non-reactive", None) == "normal"
    assert classify_qualitative("Reactive", None) == "abnormal"


def test_lab_flag_status():
    assert lab_flag_status("H") == "high" and lab_flag_status("l") == "low"
    assert lab_flag_status(None) is None and lab_flag_status("N") is None


@pytest.mark.parametrize("value,bounds,status,ref,expected", [
    ("5.3", {"low": 6.5, "high": 13}, "low", (8.6, 10.2), "low"),
    ("13.67", {"low": None, "high": 6}, "high", (0.7, 1.3), "high"),
    ("0.9", {"low": None, "high": 6}, "normal", (0.6, 1.1), None),
    ("5.9", {"low": None, "high": 6}, "high", (0.7, 1.3), None),
    ("6", {"low": None, "high": 6}, "high", (0.7, 1.3), "high"),      # inclusive
    ("5", {"low": None, "high": 6}, "low", (0.7, 1.3), None),         # a LOW value is never tested against HIGH
    ("500", {"low": None, "high": 400}, None, (None, None), "high"),  # no range: both sides are tested
    ("Positive", {"low": 1, "high": 2}, "abnormal", (None, None), None),
])
def test_critical_breach(value, bounds, status, ref, expected):
    assert critical_breach(value, bounds, status=status, ref_low=ref[0], ref_high=ref[1]) == expected


def test_normal_status_ignores_an_incoherent_critical_low():
    # critical low 500 against a 2.0-7.0 range is a unit mismatch (the v3 neutrophil bug)
    assert critical_breach("4.28", {"low": 500, "high": None}, status="normal", ref_low=2.0, ref_high=7.0) is None


def test_alert_level_follows_the_status_and_none_means_no_alert():
    assert alert_level_for("critical") == "CRITICAL"
    assert alert_level_for("high") == alert_level_for("low") == alert_level_for("abnormal") == "MEDICAL_ATTENTION"
    assert alert_level_for("normal") == "NORMAL"
    assert alert_level_for("text") is None
