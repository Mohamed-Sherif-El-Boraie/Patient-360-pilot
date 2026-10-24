"""The dispatcher: take the next ready unit, best source first, and upload it to S3 (that starts the AWS automation)."""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import structlog

from patient360.domain.common.blob_paths import batch_jsonl_path, blob_day, processed_path, s3_processed_key
from patient360.domain.rules.dispatch import retry_after
from patient360.flow.batching import build_jsonl
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
        return Dispatched(unit, "failed" if delay is None else "retry")

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
        return json.dumps(self._store.model_input_of(unit), ensure_ascii=False).encode()
