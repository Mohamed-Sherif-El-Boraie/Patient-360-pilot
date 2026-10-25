"""Trust boundary: bytes in -> Valid | Invalid. One endpoint: the payload's `source` number picks the model."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from patient360.domain.compat import frozendict
from patient360.domain.models import SymptomPayload, VisitPayload

MAX_LOC_PART = 60   # an unknown key name is sender-chosen text; keep what we store and return short

MODELS = frozendict({1: VisitPayload, 2: VisitPayload, 3: SymptomPayload, 4: VisitPayload})   # source id -> model


@dataclass(frozen=True)
class Valid:
    payload: BaseModel


@dataclass(frozen=True)
class Invalid:
    # Never contains the input value: errors are saved and returned (422), and the input is patient data.
    errors: list[dict[str, Any]] = field(default_factory=list)


def _error(loc: list[Any], msg: str, kind: str) -> dict[str, Any]:
    return {"loc": loc, "msg": msg, "type": kind}


def _reject_constant(name: str) -> None:
    raise ValueError(name)   # NaN / Infinity are not JSON, and PostgreSQL jsonb refuses them


def validate_payload(raw: bytes) -> Valid | Invalid:
    try:
        obj = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, RecursionError):   # bad JSON, bad bytes, NaN / Infinity, absurdly deep nesting
        return Invalid([_error([], "body is not valid JSON", "json_invalid")])
    source = obj.get("source") if isinstance(obj, dict) else None
    model = MODELS.get(source) if type(source) is int else None
    if model is None:
        return Invalid([_error(["source"], f"source must be one of {sorted(MODELS)}", "unknown_source")])
    try:
        payload = model.model_validate(obj, strict=True)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
        return Invalid([_error([str(part)[:MAX_LOC_PART] for part in e["loc"]], e["msg"], e["type"]) for e in errors])
    return Valid(payload)
