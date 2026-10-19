"""Small pure helpers shared by the domain logic (no I/O)."""
from __future__ import annotations

import math
import re
from typing import Any

_NOT_A_NUMBER_TAIL = re.compile(r"[:/]|-\d")  # "1:64" (titre), "5/10", "2026-01-10", "5-10"
_NUMBER_PREFIX = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


def parse_numeric(value: Any) -> int | float | None:
    """Port of v3 ``labValues.parseNumeric``: drop commas/spaces, read the leading number."""
    if value is None or value == "":
        return None
    text = re.sub(r"[,\s]", "", str(value))
    match = _NUMBER_PREFIX.match(text)
    if not match or _NOT_A_NUMBER_TAIL.match(text[match.end():]):
        return None
    number = float(match.group(0))
    if math.isnan(number) or math.isinf(number):
        return None
    return int(number) if number.is_integer() else number


def remove_empty_values(value: Any) -> Any:
    """Drop ``None``, ``""`` and empty containers recursively. Keeps ``0`` and ``False``."""
    if isinstance(value, list):
        items = [kept for kept in (remove_empty_values(item) for item in value) if kept is not None]
        return items or None
    if isinstance(value, dict):
        out = {key: kept for key, item in value.items() if (kept := remove_empty_values(item)) is not None}
        return out or None
    if value is None or value == "":
        return None
    return value
