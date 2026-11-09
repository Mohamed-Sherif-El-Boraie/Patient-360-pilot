"""Processing a received visit: build the AI input, decide tier and lane, pick the retrieved tests, store the result."""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from patient360.domain.builders.ai_input import SOURCE_NAMES, build_ai_input
from patient360.domain.builders.model_input import build_model_input, single_visit_file
from patient360.domain.common.blob_paths import blob_day, processed_path
from patient360.domain.rules.classify import decide_tier, lane_for_tier
from patient360.domain.rules.retrieval import select_retrieved_tests
from patient360.ports import BlobStore, BlobUnavailable, Catalogue, Prepared, VisitStore
from patient360.prompts import PROMPT_AND_SCHEMA_BY_SOURCE

BATCH = 2
STUCK_AFTER = timedelta(minutes=5)   # still "received" after this long: cut short by a restart, not in progress


@dataclass(frozen=True)
class Processed:
    visit_id: int
    tier: str
    lane: str
    read_from: Literal["blob", "postgresql"]


def write_model_input(blobs: BlobStore, path: str, record_id: str,
                      model_input: dict[str, Any]) -> Literal["blob", "postgresql"]:
    """Save the file AWS reads (the flat single-visit file, with the visit's key) in Blob. ``postgresql`` when Blob is
    down: the database copy (the bare request) is then the one that is read."""
    try:
        blobs.put(path, json.dumps(single_visit_file(record_id, model_input), ensure_ascii=False).encode())
    except BlobUnavailable:
        return "postgresql"
    return "blob"


class Processor:
    def __init__(self, store: VisitStore, catalogue: Catalogue, blobs: BlobStore,
                 prompts: Mapping[str, tuple[str, dict[str, Any]]] = PROMPT_AND_SCHEMA_BY_SOURCE) -> None:
        self._store, self._catalogue, self._blobs, self._prompts = store, catalogue, blobs, prompts

    def process(self, visit_id: int) -> Processed:
        """Process a saved visit (the recovery path: the visit was saved without its processing)."""
        stored = self._store.stored_visit(visit_id)
        done = self.prepare(stored.raw, stored.source_id, stored.idempotency_key, stored.received_at)
        self._store.save_processed(visit_id, done.tier, done.model_input, done.read_from)
        return Processed(visit_id, done.tier, lane_for_tier(done.tier), done.read_from)

    def prepare(self, raw: bytes, source_id: int, idempotency_key: str, received_at: datetime) -> Prepared:
        """No database write. The file made here is the full Bedrock request (``model_input``): what AWS sends to the
        model. Intake saves it with the visit; ``process`` saves it for a visit that arrived without it."""
        ai = build_ai_input(json.loads(raw))
        tier = decide_tier(ai.rows, len(ai.descriptive_rows))
        lane = lane_for_tier(tier)
        retrieved = select_retrieved_tests(ai.rows, self._catalogue.parameter_categories(),
                                           self._catalogue.tests_by_category(), ai.performed_test_names)
        patient_data = {**ai.ai_json, "retrieved_tests": retrieved} if retrieved else ai.ai_json
        system_prompt, output_schema = self._prompts[SOURCE_NAMES[source_id]]
        model_input = build_model_input(system_prompt=system_prompt, output_schema=output_schema,
                                        processed=patient_data)
        # A batch visit is not written here: its file goes inside its batch folder, once the batch exists.
        read_from = "postgresql" if source_id == BATCH else write_model_input(
            self._blobs, processed_path(SOURCE_NAMES[source_id], lane, blob_day(received_at), idempotency_key),
            idempotency_key, model_input)
        return Prepared(tier, model_input, read_from)


def process_stuck_visits(store: VisitStore, processor: Processor, now: datetime) -> list[Processed]:
    """The 5-minute timer calls this: process every visit that was accepted but never processed.
    Processing a visit twice is safe (same file, a new database version), so two instances may overlap."""
    return [processor.process(visit_id) for visit_id in store.visits_waiting_for_processing(now - STUCK_AFTER)]
