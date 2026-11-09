"""Receiving a payload from the Backend: validate, save it safely, answer. No network code: the stores are passed in."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog

from patient360.domain.builders.ai_input import SOURCE_NAMES
from patient360.domain.common.blob_paths import (
    blob_day,
    pending_path,
    raw_path,
    rejected_path,
    rejected_unknown_path,
)
from patient360.domain.common.ids import make_idempotency_key
from patient360.domain.payload.validate import Invalid, validate_payload
from patient360.domain.payload.values import as_whole_number
from patient360.failure import StepFailed, error_name, step, where
from patient360.ports import BlobStore, BlobUnavailable, Prepared, VisitStore

log = structlog.get_logger()

SYMPTOM_CHECKER = 3


@dataclass(frozen=True)
class Accepted:        # answer 202
    visit_id: int
    idempotency_key: str
    source_id: int
    processed: bool = False      # saved already processed: nothing is left to process in the background


@dataclass(frozen=True)
class Duplicate:       # answer 409, code 0: we already have it and its result was delivered
    visit_id: int


@dataclass(frozen=True)
class Retry:           # answer 200, code -1: we already have it but its result was not delivered: it is sent again
    visit_id: int


@dataclass(frozen=True)
class SymptomAgain:    # answer 200, code -1: the symptom check came again; ``data`` says the checker is closed now
    data: dict[str, Any]


@dataclass(frozen=True)
class Rejected:        # answer 422, with the error list (never the input)
    errors: list[dict[str, Any]]
    patient_id: int | None = None      # the ids that could still be read: enough to tell the Backend the patient failed
    visit_id: int | None = None
    source_id: int | None = None
    company_id: int | None = None


@dataclass(frozen=True)
class VisitNotFound:   # answer 404 VISIT_NOT_FOUND: a symptom check for a visit we do not have
    visit_id: int


@dataclass(frozen=True)
class SymptomCheckerClosed:   # answer 409 SYMPTOM_CHECKER_CLOSED: the visit's result has symptom_flag false
    visit_id: int


@dataclass(frozen=True)
class TooLarge:        # answer 413: over MAX_REQUEST_BYTES; nothing is read, saved or logged
    size: int
    limit: int


@dataclass(frozen=True)
class Parked:          # answer 202, code -1: a step of ours failed; the payload waits in Blob for the recovery
    visit_id: int


Outcome = (Accepted | Duplicate | Retry | SymptomAgain | Rejected | VisitNotFound | SymptomCheckerClosed | TooLarge
           | Parked)


def _peek(raw: bytes) -> tuple[int | None, int | None, int | None, int | None]:
    """The source number, visit id, patient id and company id of a payload that failed validation, if usable."""
    try:
        obj = json.loads(raw)
    except (ValueError, RecursionError):
        return None, None, None, None
    if not isinstance(obj, dict):
        return None, None, None, None
    source, visit, patient, company = (as_whole_number(obj.get(k))
                                       for k in ("source", "visit_id", "patient_id", "company_id"))
    source = source if type(source) is int and source in SOURCE_NAMES else None
    visit = visit if type(visit) is int and visit >= 1 else None
    patient = patient if type(patient) is int and patient >= 1 else None
    company = company if type(company) is int and company >= 1 else None
    return source, visit, patient, company


class Intake:
    def __init__(self, store: VisitStore, blobs: BlobStore, secret: bytes, now: Callable[[], datetime],
                 max_bytes: int | None = None,
                 prepare: Callable[[bytes, int, str, datetime], Prepared] | None = None) -> None:
        """``prepare``: processes a visit in memory (``Processor.prepare``). Given, a visit is saved processed in one
        database write; without it a visit is saved as received and processed afterwards."""
        self._store, self._blobs, self._secret, self._now, self._max_bytes = store, blobs, secret, now, max_bytes
        self._prepare = prepare

    def _visit_again(self, visit_id: int) -> Duplicate | Retry:
        """The same visit came again: a duplicate if its result was delivered, else a retry (a result whose delivery
        gave up is put back on the delivery queue)."""
        if self._store.result_delivered(visit_id):
            return Duplicate(visit_id)
        self._store.requeue_failed_delivery(visit_id)
        return Retry(visit_id)

    def _symptom_again(self, visit_id: int) -> SymptomAgain:
        """The symptom check came again (the wrapper started it a second time): success, and the data says the checker
        is closed now. A result that was not delivered is sent again, as for a visit."""
        if not self._store.result_delivered(visit_id, "symptom"):
            self._store.requeue_failed_delivery(visit_id, "symptom")
        raw = json.loads(self._store.stored_visit(visit_id).raw)
        return SymptomAgain({"success": True, "patient_id": raw["patient_id"], "visit_id": visit_id,
                             "company_id": raw["company_id"], "source": SYMPTOM_CHECKER, "symptom_flag": False})

    def receive(self, raw: bytes, *, symptom_check: bool | None = None, park: bool = True) -> Outcome:
        """``symptom_check``: True when the call came to the symptom-check endpoint, False for the visit endpoint (the
        other kind of payload is refused); None accepts both. ``park``: when a step fails after the payload was
        validated (the database is down, say), keep the payload in Blob and answer ``Parked``, which the sender sees as
        accepted; the recovery saves it later. Without ``park`` the failure is raised (the recovery itself)."""
        if self._max_bytes is not None and len(raw) > self._max_bytes:
            return TooLarge(len(raw), self._max_bytes)
        received_at = self._now()
        with step("validating the payload"):
            result = validate_payload(raw)
        if isinstance(result, Invalid):
            return self._reject(raw, result.errors, received_at)
        payload = result.payload.model_dump()
        source_id, visit_id = payload["source"], payload["visit_id"]
        if symptom_check is not None and (source_id == SYMPTOM_CHECKER) != symptom_check:
            wanted = [SYMPTOM_CHECKER] if symptom_check else sorted(set(SOURCE_NAMES) - {SYMPTOM_CHECKER})
            return self._reject(raw, [{"loc": ["source"], "msg": f"source must be one of {wanted} on this endpoint",
                                       "type": "wrong_endpoint"}], received_at)
        try:
            return self._save(raw, payload, received_at)
        except StepFailed as failed:        # our fault, not the sender's: keep the payload and finish it internally
            if park and self._park(raw, visit_id, received_at, failed):
                return Parked(visit_id)
            raise

    def _save(self, raw: bytes, payload: dict[str, Any], received_at: datetime) -> Outcome:
        """Everything after validation: the duplicate checks, the blob and the database."""
        source_id, visit_id, patient_id = payload["source"], payload["visit_id"], payload["patient_id"]
        if source_id == SYMPTOM_CHECKER:
            with step("reading the symptom-check state of the visit from the database", visit_id):
                state = self._store.symptom_check_state(visit_id, patient_id)
            if state == "no_visit":
                return VisitNotFound(visit_id)
            if state == "already_used":
                with step("answering a symptom check that came again", visit_id):
                    return self._symptom_again(visit_id)
            if state == "closed":       # nothing is saved: the patient is told at once instead of waiting for no answer
                return SymptomCheckerClosed(visit_id)
        else:
            with step("checking in the database whether the visit already exists", visit_id):
                exists = self._store.visit_exists(visit_id)
            if exists:
                with step("answering a visit that came again", visit_id):
                    return self._visit_again(visit_id)
        # A symptom check has no company_id: it keeps the key its stored visit already got.
        with step("making the visit key", visit_id):
            key = (self._store.stored_visit(visit_id).idempotency_key if source_id == SYMPTOM_CHECKER
                   else make_idempotency_key(self._secret, patient_id=patient_id, visit_id=visit_id,
                                             company_id=payload["company_id"]))
        with step("saving the raw payload in the blob", visit_id):
            read_from = self._save_raw_to_blob(SOURCE_NAMES[source_id], key, raw, received_at)
        processed = None
        if source_id == SYMPTOM_CHECKER:
            with step("saving the symptom check in the database", visit_id):
                self._store.save_symptom_check(payload, raw, received_at, read_from)
        else:
            processed = self._prepare_visit(source_id, key, raw, received_at)
            with step("saving the visit in the database", visit_id):
                self._store.save_visit(payload, raw, key, received_at, read_from, processed)
        return Accepted(visit_id, key, source_id, processed is not None)   # only after the database has it

    def _park(self, raw: bytes, visit_id: int, received_at: datetime, failed: StepFailed) -> bool:
        """Keep a valid payload in Blob under ``_pending``. ``False`` when Blob cannot keep it either: nothing is safe
        then, so the failure is raised and the sender resends."""
        try:
            self._blobs.put(pending_path(blob_day(received_at), visit_id, hashlib.sha256(raw).hexdigest()), raw)
        except BlobUnavailable:
            return False
        log.warning("visit_parked", step=failed.step, visit_id=visit_id, error_type=error_name(failed.error),
                    where=where(failed.error))
        return True

    def _prepare_visit(self, source_id: int, key: str, raw: bytes, received_at: datetime) -> Prepared | None:
        """Process the visit in memory so it is saved with one database write. If that fails the visit is still
        saved, without it, and the background processing (or the 5-minute sweep) does it later."""
        if self._prepare is None:
            return None
        try:
            return self._prepare(raw, source_id, key, received_at)
        except Exception as error:
            log.warning("prepare_failed", step="processing the visit in memory", error_type=error_name(error),
                        where=where(error), visit_key=key)
            return None

    def _save_raw_to_blob(self, source: str, key: str, raw: bytes, received_at: datetime) -> str:
        """"blob" if the file is in Blob (or was already), else "postgresql": PostgreSQL has the bytes either way."""
        try:
            self._blobs.put(raw_path(source, blob_day(received_at), key), raw)
        except BlobUnavailable:
            return "postgresql"
        return "blob"

    def _reject(self, raw: bytes, errors: list[dict[str, Any]], received_at: datetime) -> Rejected:
        source_id, visit_id, patient_id, company_id = _peek(raw)
        day = blob_day(received_at)
        if source_id is not None and visit_id is not None:
            path = rejected_path(SOURCE_NAMES[source_id], day, visit_id)
        else:
            path = rejected_unknown_path(day, hashlib.sha256(raw).hexdigest())
        try:
            self._blobs.put(path, raw)
        except BlobUnavailable:
            path = None
        try:        # the sender's mistake is answered 422 whatever happens here: the copy is only for us to look at
            self._store.save_rejected(source_id, raw, errors, path)
        except Exception as error:
            log.warning("rejected_payload_not_saved", step="saving the rejected payload in the database",
                        visit_id=visit_id, error_type=error_name(error), where=where(error))
        return Rejected(errors, patient_id, visit_id, source_id, company_id)
