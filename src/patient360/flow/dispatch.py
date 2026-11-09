"""The dispatcher: take the next ready unit, best source first, and upload it to S3 (that starts the AWS automation)."""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import structlog

from patient360.domain.builders.ai_input import build_ai_input
from patient360.domain.builders.backend_output import build_failed_output
from patient360.domain.builders.model_input import single_visit_file
from patient360.domain.common.blob_paths import batch_jsonl_path, blob_day, processed_path, s3_processed_key
from patient360.domain.rules.dispatch import retry_after
from patient360.failure import error_name, where
from patient360.flow.batching import build_jsonl
from patient360.flow.intake import SYMPTOM_CHECKER
from patient360.ports import BlobStore, BlobUnavailable, S3Store, S3Unavailable, VisitStore, WorkUnit

log = structlog.get_logger()


@dataclass(frozen=True)
class Dispatched:
    unit: WorkUnit
    outcome: Literal["sent", "retry", "failed"]


class Dispatcher:
    def __init__(self, store: VisitStore, blobs: BlobStore, s3: S3Store, now: Callable[[], datetime]) -> None:
        self._store, self._blobs, self._s3, self._now = store, blobs, s3, now

    def run(self) -> list[Dispatched]:
        """Send every unit that is ready. A unit that failed waits for its retry time, so the loop always ends."""
        done = []
        while (unit := self._store.claim_next_work(self._now())) is not None:
            done.append(self._send(unit))
        return done

    def _send(self, unit: WorkUnit) -> Dispatched:
        day = blob_day(unit.created_at)
        key = s3_processed_key(unit.source, unit.lane, day, unit.idempotency_key, unit.batch_id)
        try:
            self._s3.put(unit.source, key, self._load(unit, day))
        except S3Unavailable as error:
            return self._retry_later(unit, error)
        self._store.mark_sent(unit)
        log.info("sent_to_aws", source=unit.source, lane=unit.lane, attempt=unit.attempts + 1)
        return Dispatched(unit, "sent")

    def _retry_later(self, unit: WorkUnit, error: Exception) -> Dispatched:
        attempt = unit.attempts + 1
        delay = retry_after(attempt)
        self._store.mark_failed_attempt(unit, type(error).__name__, None if delay is None else self._now() + delay)
        log.warning("s3_upload_failed", source=unit.source, attempt=attempt, error_type=type(error).__name__,
                    status="failed" if delay is None else "retry")
        if delay is None:
            self._tell_failed(unit)
        return Dispatched(unit, "failed" if delay is None else "retry")

    def _tell_failed(self, unit: WorkUnit) -> None:
        """The upload gave up, so AWS will never answer: the Backend gets ``success`` false (as when AWS fails), for the
        visit, or for every visit of the batch. One visit failing here never stops the others."""
        visit_ids = self._store.batch_visit_ids(unit.batch_id) if unit.batch_id else [unit.visit_id]
        for visit_id in visit_ids:
            try:
                stored = self._store.stored_visit(visit_id)
                raw = json.loads(stored.raw)
                self._store.save_failed_output(visit_id, build_failed_output(
                    patient_id=raw["patient_id"], visit_id=visit_id, company_id=raw["company_id"],
                    source=SYMPTOM_CHECKER if unit.analysis == "symptom" else stored.source_id,
                    rows=build_ai_input(raw).rows), unit.analysis)
            except Exception as error:
                log.warning("failed_result_not_saved", step="telling the Backend the upload failed", visit_id=visit_id,
                            error_type=error_name(error), where=where(error))

    def _load(self, unit: WorkUnit, day) -> bytes:
        """The file to upload: from Blob when that is where it was saved, else rebuilt from PostgreSQL (and put back in
        Blob if it can be)."""
        path = (batch_jsonl_path(unit.lane, day, unit.batch_id) if unit.batch_id
                else processed_path(unit.source, unit.lane, day, unit.idempotency_key))
        if unit.read_from == "blob":
            try:
                saved = self._blobs.get(path)
            except BlobUnavailable:
                saved = None
            if saved is not None:
                return saved
        data = self._rebuild(unit)
        try:
            self._blobs.put(path, data)
        except BlobUnavailable:
            pass
        return data

    def _rebuild(self, unit: WorkUnit) -> bytes:
        if unit.batch_id:
            return build_jsonl(self._store.batch_model_inputs(unit.batch_id)).encode()
        record = single_visit_file(unit.idempotency_key, self._store.model_input_of(unit))   # PostgreSQL holds it bare
        return json.dumps(record, ensure_ascii=False).encode()
