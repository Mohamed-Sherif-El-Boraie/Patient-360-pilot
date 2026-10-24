"""Processing a received visit: build the AI input, decide tier and lane, pick the retrieved tests, store the result."""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from patient360.domain.builders.ai_input import SOURCE_NAMES, build_ai_input
from patient360.domain.builders.model_input import build_model_input
from patient360.domain.common.blob_paths import blob_day, processed_path
from patient360.domain.rules.classify import decide_tier, lane_for_tier
from patient360.domain.rules.retrieval import select_retrieved_tests
from patient360.ports import BlobStore, BlobUnavailable, Catalogue, VisitStore
from patient360.prompts import PROMPT_AND_SCHEMA_BY_SOURCE

BATCH = 2
STUCK_AFTER = timedelta(minutes=5)   # still "received" after this long: cut short by a restart, not in progress


@dataclass(frozen=True)
class Processed:
    visit_id: int
    tier: str
    lane: str
    read_from: Literal["blob", "postgresql"]


def write_model_input(blobs: BlobStore, path: str, model_input: dict[str, Any]) -> Literal["blob", "postgresql"]:
    """Save the request in Blob. ``postgresql`` when Blob is down: the database copy is then the one that is read."""
    try:
        blobs.put(path, json.dumps(model_input, ensure_ascii=False).encode())
    except BlobUnavailable:
        return "postgresql"
    return "blob"


class Processor:
    def __init__(self, store: VisitStore, catalogue: Catalogue, blobs: BlobStore,
                 prompts: Mapping[str, tuple[str, dict[str, Any]]] = PROMPT_AND_SCHEMA_BY_SOURCE) -> None:
        self._store, self._catalogue, self._blobs, self._prompts = store, catalogue, blobs, prompts

    def process(self, visit_id: int) -> Processed:
        """The file made here is the full Bedrock request (``model_input``): what AWS sends to the model."""
        stored = self._store.stored_visit(visit_id)
        ai = build_ai_input(json.loads(stored.raw))
        tier = decide_tier(ai.rows, len(ai.descriptive_rows))
        lane = lane_for_tier(tier)
        retrieved = select_retrieved_tests(ai.rows, self._catalogue.parameter_categories(),
                                           self._catalogue.tests_by_category(), ai.performed_test_names)
        patient_data = {**ai.ai_json, "retrieved_tests": retrieved} if retrieved else ai.ai_json
        system_prompt, output_schema = self._prompts[SOURCE_NAMES[stored.source_id]]
        model_input = build_model_input(system_prompt=system_prompt, output_schema=output_schema,
                                        processed=patient_data)
        # A batch visit is not written here: its file goes inside its batch folder, once the batch exists.
        read_from = "postgresql" if stored.source_id == BATCH else write_model_input(
            self._blobs, processed_path(SOURCE_NAMES[stored.source_id], lane, blob_day(stored.received_at),
                                        stored.idempotency_key), model_input)
        self._store.save_processed(visit_id, tier, model_input, read_from)
        return Processed(visit_id, tier, lane, read_from)


def process_stuck_visits(store: VisitStore, processor: Processor, now: datetime) -> list[Processed]:
    """The 5-minute timer calls this: process every visit that was accepted but never processed.
    Processing a visit twice is safe (same file, a new database version), so two instances may overlap."""
    return [processor.process(visit_id) for visit_id in store.visits_waiting_for_processing(now - STUCK_AFTER)]
