"""Patient tier and model lane. Pure port of v3 ``severity.decideTier`` + ``modelRouter``."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from patient360.domain.compat import frozendict
from patient360.domain.logic import ABNORMAL

# normal = Haiku lane, critical = Sonnet lane (BEDROCK_LARGE_MODEL_TIERS default in v3 = MODERATE,CRITICAL)
LANE_BY_TIER = frozendict({"NORMAL": "normal", "MILD": "normal", "MODERATE": "critical", "CRITICAL": "critical"})


def _has_result(row: Mapping[str, Any]) -> bool:
    return str(row.get("value") if row.get("value") is not None else "").strip() != ""


def classify_patient(flat: Sequence[Mapping[str, Any]]) -> str:
    """First matching rule wins: critical > unrangeable (fail safe) > 3+ abnormal > 1-2 abnormal > normal."""
    rows = [row for row in flat if _has_result(row)]
    if not rows:  # no data is not "no findings": escalate rather than call it normal
        return "MODERATE"
    if any(row["is_critical"] for row in rows):
        return "CRITICAL"
    if any(row["status"] is None for row in rows):
        return "MODERATE"
    abnormal = sum(1 for row in rows if row["status"] in ABNORMAL)
    if abnormal >= 3:
        return "MODERATE"
    return "MILD" if abnormal else "NORMAL"


def lane_for(tier: str) -> str:
    return LANE_BY_TIER[tier]
