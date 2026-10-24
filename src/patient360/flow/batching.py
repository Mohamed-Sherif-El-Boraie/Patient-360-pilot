"""The batch trigger: when a full batch (``BATCH_SIZE``, 100 by default) of one lane is waiting, make a batch."""
from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from patient360.config.settings import DEFAULT_BATCH_SIZE
from patient360.domain.common.blob_paths import batch_jsonl_path, blob_day, processed_path
from patient360.ports import BatchVisit, BlobStore, BlobUnavailable, ClaimedBatch, VisitStore

LANES = ("normal", "critical")


@dataclass(frozen=True)
class CreatedBatch:
    batch_id: str
    lane: str
    read_from: Literal["blob", "postgresql"]


def _dump(document: dict) -> str:
    return json.dumps(document, ensure_ascii=False)   # one line: newlines inside text are escaped


def build_jsonl(visits: Sequence[BatchVisit]) -> str:
    """The file for AWS: one line per visit. Only Blob and S3 hold it; PostgreSQL holds the visits, so a lost one can
    be rebuilt."""
    return "\n".join(_dump({"recordId": v.idempotency_key, "modelInput": v.model_input}) for v in visits)


class BatchTrigger:
    def __init__(self, store: VisitStore, blobs: BlobStore, size: int = DEFAULT_BATCH_SIZE) -> None:
        self._store, self._blobs, self._size = store, blobs, size

    def run(self) -> list[CreatedBatch]:
        """One tick: every lane makes as many full batches as it has visits for."""
        created = []
        for lane in LANES:
            while (claimed := self._store.claim_batch(lane, self._size)) is not None:
                created.append(self._write(lane, claimed))
        return created

    def _write(self, lane: str, claimed: ClaimedBatch) -> CreatedBatch:
        """The batch folder holds one file per visit and the JSONL, named by the day the batch was made.
        The JSONL is written last: when it exists, the visit files exist too."""
        day = blob_day(claimed.created_at)
        files = {processed_path("batch", lane, day, v.idempotency_key, claimed.batch_id): _dump(v.model_input)
                 for v in claimed.visits}
        files[batch_jsonl_path(lane, day, claimed.batch_id)] = build_jsonl(claimed.visits)
        try:
            for path, text in files.items():
                self._blobs.put(path, text.encode())
            read_from = "blob"
        except BlobUnavailable:
            read_from = "postgresql"
        self._store.set_batch_read_from(claimed.batch_id, read_from)
        return CreatedBatch(claimed.batch_id, lane, read_from)
