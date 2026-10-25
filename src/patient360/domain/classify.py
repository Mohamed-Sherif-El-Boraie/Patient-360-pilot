"""Patient tier and model lane. A pure rule over the statuses of all parameters."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from patient360.domain.compat import frozendict

# normal lane = cheaper model, critical lane = stronger model; also the Blob folder (normal / critical)
LANE_BY_TIER = frozendict({"NORMAL": "normal", "MILD": "normal", "MODERATE": "critical", "CRITICAL": "critical"})
MILD_MAX = 3   # 1 to 3 parameters that are not normal = MILD, more = MODERATE


def has_value(row: Mapping[str, Any]) -> bool:
    return str(row.get("value") if row.get("value") is not None else "").strip() != ""


def decide_tier(result_rows: Sequence[Mapping[str, Any]], descriptive_count: int = 0) -> str:
    """CRITICAL if any parameter is critical; else by how many are high, low, abnormal or text.

    Each descriptive result counts as one "text". A visit with no numeric parameter at all (only descriptive results,
    or nothing) is MODERATE: no data is not "no findings", so escalate.
    """
    rows = [row for row in result_rows if has_value(row)]
    if not rows:
        return "MODERATE"
    if any(row["status"] == "critical" for row in rows):
        return "CRITICAL"
    not_normal = sum(1 for row in rows if row["status"] != "normal") + descriptive_count
    if not_normal == 0:
        return "NORMAL"
    return "MILD" if not_normal <= MILD_MAX else "MODERATE"


def lane_for_tier(tier: str) -> str:
    return LANE_BY_TIER[tier]
