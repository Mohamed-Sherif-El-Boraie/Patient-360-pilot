"""Receiving a payload from the Backend: validate, save it safely, answer. No network code: the stores are passed in."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from patient360.domain.builders.ai_input import SOURCE_NAMES
from patient360.domain.common.blob_paths import blob_day, raw_path, rejected_path, rejected_unknown_path
from patient360.domain.common.ids import make_idempotency_key
from patient360.domain.payload.validate import Invalid, validate_payload
from patient360.ports import BlobStore, BlobUnavailable, VisitStore

SYMPTOM_CHECKER = 3


@dataclass(frozen=True)
class Accepted:        # answer 202
    visit_id: int
    idempotency_key: str


@dataclass(frozen=True)
class Duplicate:       # answer 200: we already have it
    visit_id: int


@dataclass(frozen=True)
class Rejected:        # answer 422, with the error list (never the input)
    errors: list[dict[str, Any]]


@dataclass(frozen=True)
class VisitNotFound:   # answer 404 VISIT_NOT_FOUND: a symptom check for a visit we do not have
    visit_id: int


def _peek(raw: bytes) -> tuple[int | None, int | None]:
    """The source number and visit id of a payload that failed validation, if they are usable."""
    try:
        obj = json.loads(raw)
    except (ValueError, RecursionError):
        return None, None
    if not isinstance(obj, dict):
        return None, None
    source, visit = obj.get("source"), obj.get("visit_id")
    source = source if type(source) is int and source in SOURCE_NAMES else None
    visit = visit if type(visit) is int and visit >= 1 else None
    return source, visit


class Intake:
    def __init__(self, store: VisitStore, blobs: BlobStore, secret: bytes, now: Callable[[], datetime]) -> None:
        self._store, self._blobs, self._secret, self._now = store, blobs, secret, now

    def receive(self, raw: bytes) -> Accepted | Duplicate | Rejected | VisitNotFound:
        received_at = self._now()
        result = validate_payload(raw)
        if isinstance(result, Invalid):
            return self._reject(raw, result.errors, received_at)
        payload = result.payload.model_dump()
        source_id, visit_id, patient_id = payload["source"], payload["visit_id"], payload["patient_id"]
        if source_id == SYMPTOM_CHECKER:
            state = self._store.symptom_check_state(visit_id, patient_id)
            if state == "no_visit":
                return VisitNotFound(visit_id)
            if state == "already_used":
                return Duplicate(visit_id)
        elif self._store.visit_exists(visit_id):
            return Duplicate(visit_id)
        key = make_idempotency_key(self._secret, patient_id=patient_id, visit_id=visit_id)
        read_from = self._save_raw_to_blob(SOURCE_NAMES[source_id], key, raw, received_at)
        if source_id == SYMPTOM_CHECKER:
            self._store.save_symptom_check(payload, raw, received_at, read_from)
        else:
            self._store.save_visit(payload, raw, key, received_at, read_from)
        return Accepted(visit_id, key)   # only after the database has it: the answer means "safely saved"

    def _save_raw_to_blob(self, source: str, key: str, raw: bytes, received_at: datetime) -> str:
        """"blob" if the file is in Blob (or was already), else "postgresql": PostgreSQL has the bytes either way."""
        try:
            self._blobs.put(raw_path(source, blob_day(received_at), key), raw)
        except BlobUnavailable:
            return "postgresql"
        return "blob"

    def _reject(self, raw: bytes, errors: list[dict[str, Any]], received_at: datetime) -> Rejected:
        source_id, visit_id = _peek(raw)
        day = blob_day(received_at)
        if source_id is not None and visit_id is not None:
            path = rejected_path(SOURCE_NAMES[source_id], day, visit_id)
        else:
            path = rejected_unknown_path(day, hashlib.sha256(raw).hexdigest())
        try:
            self._blobs.put(path, raw)
        except BlobUnavailable:
            path = None
        self._store.save_rejected(source_id, raw, errors, path)
        return Rejected(errors)
