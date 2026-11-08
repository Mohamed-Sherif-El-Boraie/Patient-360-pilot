"""Trust boundary: bytes in -> Valid | Invalid. One endpoint: the payload's `source` number picks the model."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from patient360.domain.common.compat import frozendict
from patient360.domain.payload.explain import MAX_LOC_PART, explain
from patient360.domain.payload.models import SymptomPayload, VisitPayload
from patient360.domain.payload.values import as_whole_number, blank_to_none

MODELS = frozendict({1: VisitPayload, 2: VisitPayload, 3: SymptomPayload, 4: VisitPayload})   # source id -> model


@dataclass(frozen=True)
class Valid:
    payload: BaseModel


@dataclass(frozen=True)
class Invalid:
    # Never contains the input value: errors are saved and returned (422), and the input is patient data.
    errors: list[dict[str, Any]] = field(default_factory=list)


def _error(loc: list[Any], msg: str, kind: str, message: str | None = None) -> dict[str, Any]:
    """``message`` is the readable sentence (see ``explain``); without it the answer shows ``loc: msg``."""
    return {"loc": loc, "msg": msg, "type": kind} | ({"message": message} if message else {})


def _reject_constant(name: str) -> None:
    raise ValueError(name)   # NaN / Infinity are not JSON, and PostgreSQL jsonb refuses them


def validate_payload(raw: bytes) -> Valid | Invalid:
    try:
        obj = blank_to_none(json.loads(raw, parse_constant=_reject_constant))     # blank means null, before any check
    except (ValueError, RecursionError):   # bad JSON, bad bytes, NaN / Infinity, absurdly deep nesting
        return Invalid([_error([], "body is not valid JSON", "json_invalid")])
    source = as_whole_number(obj.get("source")) if isinstance(obj, dict) else None
    model = MODELS.get(source) if type(source) is int else None
    if model is None:
        return Invalid([_error(["source"], f"source must be one of {sorted(MODELS)}", "unknown_source")])
    obj["source"] = source      # "3" arrives as 3: the models are strict
    try:
        payload = model.model_validate(obj, strict=True)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
        return Invalid([_error([str(part)[:MAX_LOC_PART] for part in e["loc"]], e["msg"], e["type"],
                               explain(obj, e["loc"], e["type"], e["msg"])) for e in errors])
    return Valid(payload)
