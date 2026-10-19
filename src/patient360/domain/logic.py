"""Status, alert and critical-direction rules. Pure port of v3 ``labValues.js`` / ``resultClassification``.

Rule order for a result's status (first answer wins):
  1. the numeric range (CONVLOW/CONVHIGH -> NORMAL*VALUE -> numbers read from the range text)
  2. the wording of a qualitative result (negative / positive vocabulary)
  3. the laboratory's own flag (H -> high, L -> low); otherwise None = "not judged"
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from patient360.domain.values import parse_numeric

ABNORMAL = frozenset({"high", "low", "abnormal"})
# LIMS markers for "no range recorded". They count only as a PAIR: a lone -1 or 9999 is a real number.
NOT_SET_PAIRS = ((0, 0), (-1, 9999), (-1, 99999999))
CRITICAL_NOT_SET_LOWS = frozenset({-1, -9999, -99999})
CRITICAL_NOT_SET_HIGHS = frozenset({9999, 99999, 999999})

_MUTATION = r"(?:mutation|mutations|deletion|deletions|variant|variants)"
NEGATIVE_PATTERNS = (
    re.compile(r"\bnegative\b"),
    re.compile(r"\b(?:not|non)\s+detected\b"),
    re.compile(r"\bnone\s+detected\b"),
    re.compile(r"\b(?:non[\s-]?|not\s+)reactive\b"),
    re.compile(rf"\bno\s+(?:evidence\s+of\s+)?{_MUTATION}\b"),
)
# Negative clauses are removed before the positive check, so "not detected" is not read as "detected".
_NEGATIVE_CLAUSES = (
    re.compile(r"\bno\s+(?:[a-z0-9/-]+\s+){0,8}(?:detected|found|identified|present)\b"),
    re.compile(r"\b(?:not|non)\s+detected\b"),
    re.compile(r"\bnone\s+detected\b"),
    re.compile(r"\b(?:non[\s-]?|not\s+)reactive\b"),
    re.compile(rf"\bno\s+(?:evidence\s+of\s+)?{_MUTATION}\b"),
    re.compile(r"\bnegative\b"),
)
POSITIVE_PATTERN = re.compile(
    rf"\bpositive\b|\breactive\b|\bdetected\b|\b{_MUTATION}\s+(?:found|identified|present)\b"
)

_TWO_SIDED = re.compile(r"^(-?\d+(?:\.\d+)?)\s*(?:-|to)\s*(-?\d+(?:\.\d+)?)$", re.I)
_UPPER_ONLY = re.compile(r"^(?:<=?|≤|up to|less than|below)\s*(-?\d+(?:\.\d+)?)$", re.I)
_LOWER_ONLY = re.compile(r"^(?:>=?|≥|more than|above|over)\s*(-?\d+(?:\.\d+)?)$", re.I)
_DASHES = re.compile("[‐-―−]")


def reference_bounds(low: Any, high: Any) -> tuple[int | float | None, int | float | None]:
    """Numeric bounds, or (None, None) when the pair is a LIMS "not set" marker."""
    num_low, num_high = parse_numeric(low), parse_numeric(high)
    if (num_low, num_high) in NOT_SET_PAIRS:
        return None, None
    return num_low, num_high


def parse_text_range(text: Any) -> tuple[int | float | None, int | float | None]:
    """Read a range out of text such as ``6.7 - 22.4``, ``< 5.7`` or ``up to 30``; nothing else."""
    if text is None:
        return None, None
    t = _DASHES.sub("-", str(text).strip())
    t = re.sub(r"\s+", " ", t)
    if not t:
        return None, None
    if m := _TWO_SIDED.match(t):
        low, high = float(m.group(1)), float(m.group(2))
        low, high = (low, high) if low <= high else (high, low)
        return parse_numeric(low), parse_numeric(high)
    if m := _UPPER_ONLY.match(t):
        return None, parse_numeric(m.group(1))
    if m := _LOWER_ONLY.match(t):
        return parse_numeric(m.group(1)), None
    return None, None


def classify_result(value: Any, low: Any, high: Any) -> str | None:
    """``low`` / ``high`` / ``normal`` from arithmetic, or None. A bound that is None is never compared."""
    number = parse_numeric(value)
    if number is None:
        return None
    lo, hi = reference_bounds(low, high)
    if lo is not None and number < lo:
        return "low"
    if hi is not None and number > hi:
        return "high"
    if lo is not None or hi is not None:
        return "normal"
    return None


def normalise_text(text: Any) -> str:
    return " ".join(str(text if text is not None else "").split()).lower()


def polarity(text: Any) -> str | None:
    """``negative`` / ``positive`` / ``conflicting`` / None for a word result."""
    value = normalise_text(text)
    if not value:
        return None
    has_negative = any(p.search(value) for p in NEGATIVE_PATTERNS)
    remainder = value
    for clause in _NEGATIVE_CLAUSES:
        remainder = clause.sub(" ", remainder)
    has_positive = bool(POSITIVE_PATTERN.search(remainder))
    if has_negative and has_positive:
        return "conflicting"
    if has_negative:
        return "negative"
    if has_positive:
        return "positive"
    return None


def classify_qualitative(value: Any, expected_text: Any) -> str | None:
    """``normal`` / ``abnormal`` for a word result. ``abnormal`` has NO direction."""
    result = normalise_text(value)
    if not result:
        return None
    kind = polarity(result)
    if kind == "conflicting":
        return None
    if kind == "negative":
        return "normal"
    if kind == "positive":
        return "abnormal"
    expected = normalise_text(expected_text)
    return "normal" if expected and result == expected else None


def lab_flag_status(flag: Any) -> str | None:
    return {"H": "high", "L": "low"}.get(str(flag or "").strip().upper())


def critical_bounds(low: Any, high: Any) -> tuple[int | float | None, int | float | None]:
    """Numeric critical limits, or (None, None) when low and high are BOTH "no limit" markers.

    ``{-1, 6}`` keeps -1 (a real limit); ``{-1, 9999}`` means no critical limits at all.
    """
    num_low, num_high = parse_numeric(low), parse_numeric(high)
    if num_low in CRITICAL_NOT_SET_LOWS and num_high in CRITICAL_NOT_SET_HIGHS:
        return None, None
    return num_low, num_high


def _coherent_sides(bounds: Mapping[str, Any], ref_low: Any, ref_high: Any) -> tuple[bool, bool]:
    low, high = bounds.get("low"), bounds.get("high")
    low_ok = low is not None and ref_high is not None and low <= ref_high
    high_ok = high is not None and ref_low is not None and high >= ref_low
    return low_ok, high_ok


def critical_breach(
    value: Any, bounds: Mapping[str, Any] | None, *, status: str | None,
    ref_low: Any = None, ref_high: Any = None,
) -> str | None:
    """``low`` / ``high`` / None. Direction-aware, inclusive on both bounds.

    A value already known to be HIGH is only compared with the critical HIGH bound (and LOW with LOW).
    ``normal`` is checked only on sides whose bound is coherent with the reference range.
    No status (no usable range) and ``abnormal`` are checked on both sides.
    """
    number = parse_numeric(value)
    if number is None or not bounds:
        return None
    if status == "normal":
        check_low, check_high = _coherent_sides(bounds, ref_low, ref_high)
    else:
        check_low = status in ("low", "abnormal", None)
        check_high = status in ("high", "abnormal", None)
    low, high = bounds.get("low"), bounds.get("high")
    if check_low and low is not None and number <= low:
        return "low"
    if check_high and high is not None and number >= high:
        return "high"
    return None


def alert_for(status: str | None, direction: str | None) -> str:
    if direction:
        return "CRITICAL"
    if status in ABNORMAL:
        return "MEDICAL_ATTENTION"
    if status == "normal":
        return "NORMAL"
    return "NOT_CLASSIFIED"
