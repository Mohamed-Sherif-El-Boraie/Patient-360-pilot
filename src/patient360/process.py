"""Processing a received visit: build the AI input, decide tier and lane, pick the retrieved tests, store the result."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from patient360.domain.ai_input import SOURCE_NAMES, build_ai_input
from patient360.domain.blob_paths import blob_day, processed_path
from patient360.domain.classify import decide_tier, lane_for_tier
from patient360.domain.retrieval import select_retrieved_tests
from patient360.ports import BlobStore, BlobUnavailable, Catalogue, StoredVisit, VisitStore

BATCH = 2


@dataclass(frozen=True)
class Processed:
    visit_id: int
    tier: str
    lane: str
    read_from: Literal["blob", "postgresql"]


class Processor:
    def __init__(self, store: VisitStore, catalogue: Catalogue, blobs: BlobStore) -> None:
        self._store, self._catalogue, self._blobs = store, catalogue, blobs

    def process(self, visit_id: int) -> Processed:
        stored = self._store.stored_visit(visit_id)
        ai = build_ai_input(json.loads(stored.raw))
        tier = decide_tier(ai.rows, len(ai.descriptive_rows))
        lane = lane_for_tier(tier)
        retrieved = select_retrieved_tests(ai.rows, self._catalogue.parameter_categories(),
                                           self._catalogue.tests_by_category(), ai.performed_test_names)
        processed = {**ai.ai_json, "retrieved_tests": retrieved} if retrieved else ai.ai_json
        read_from = "postgresql" if stored.source_id == BATCH else self._write_to_blob(stored, lane, processed)
        self._store.save_processed(visit_id, tier, processed, read_from)
        return Processed(visit_id, tier, lane, read_from)

    def _write_to_blob(self, stored: StoredVisit, lane: str, processed: dict[str, Any]) -> str:
        """A batch visit is not written here: its file goes inside its batch folder, once the batch exists."""
        path = processed_path(SOURCE_NAMES[stored.source_id], lane, blob_day(stored.received_at),
                              stored.idempotency_key)
        try:
            self._blobs.put(path, json.dumps(processed, ensure_ascii=False).encode())
        except BlobUnavailable:
            return "postgresql"
        return "blob"
