"""The batch trigger: when 100 batch visits of one lane are waiting, make a batch and write its files."""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from patient360.domain.blob_paths import batch_jsonl_path, blob_day, processed_path
from patient360.ports import BlobStore, BlobUnavailable, ClaimedBatch, VisitStore

BATCH_SIZE = 100
LANES = ("normal", "critical")


@dataclass(frozen=True)
class CreatedBatch:
    batch_id: str
    lane: str
    jsonl: bytes                              # also kept here, so sending to AWS does not depend on Blob
    read_from: Literal["blob", "postgresql"]


def _dump(document: dict) -> str:
    return json.dumps(document, ensure_ascii=False)   # one line: newlines inside text are escaped


class BatchTrigger:
    def __init__(self, store: VisitStore, blobs: BlobStore, now: Callable[[], datetime],
                 size: int = BATCH_SIZE) -> None:
        self._store, self._blobs, self._now, self._size = store, blobs, now, size

    def run(self) -> list[CreatedBatch]:
        """One tick: every lane makes as many full batches as it has visits for."""
        created = []
        for lane in LANES:
            while (claimed := self._store.claim_batch(lane, self._size)) is not None:
                created.append(self._write(lane, claimed))
        return created

    def _write(self, lane: str, claimed: ClaimedBatch) -> CreatedBatch:
        """The batch folder holds one file per visit and the 100-line file for AWS, named by the day the batch was made.
        The JSONL is written last: when it exists, the visit files exist too."""
        day = blob_day(self._now())
        jsonl = "\n".join(_dump({"recordId": v.idempotency_key, "document": v.processed}) for v in claimed.visits)
        files = {processed_path("batch", lane, day, v.idempotency_key, claimed.batch_id): _dump(v.processed)
                 for v in claimed.visits}
        files[batch_jsonl_path(lane, day, claimed.batch_id)] = jsonl
        try:
            for path, text in files.items():
                self._blobs.put(path, text.encode())
            read_from = "blob"
        except BlobUnavailable:
            read_from = "postgresql"
        self._store.set_batch_read_from(claimed.batch_id, read_from)
        return CreatedBatch(claimed.batch_id, lane, jsonl.encode(), read_from)
