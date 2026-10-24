"""The symptom check: a second AI run for a visit that already has its result. Same steps as a visit, other input."""
from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from patient360.domain.builders.ai_input import build_ai_input
from patient360.domain.builders.model_input import build_model_input
from patient360.domain.builders.symptom_ai_input import build_symptom_ai_input
from patient360.domain.common.blob_paths import blob_day, processed_path
from patient360.domain.rules.classify import decide_tier, lane_for_tier
from patient360.domain.rules.retrieval import select_retrieved_tests
from patient360.flow.process import STUCK_AFTER, Processed, write_model_input
from patient360.ports import BlobStore, Catalogue, VisitStore
from patient360.prompts import PROMPT_AND_SCHEMA_BY_SOURCE

SOURCE = "symptom-checker"


class SymptomProcessor:
    def __init__(self, store: VisitStore, catalogue: Catalogue, blobs: BlobStore,
                 prompts: Mapping[str, tuple[str, dict[str, Any]]] = PROMPT_AND_SCHEMA_BY_SOURCE) -> None:
        self._store, self._catalogue, self._blobs, self._prompts = store, catalogue, blobs, prompts

    def process(self, visit_id: int) -> Processed | None:
        """``None`` while the visit has no result yet: the symptom check needs it ("previous_ai_analysis"), so it
        waits and the timer tries again. The lane is the lane of the visit."""
        visit_ai = self._store.visit_ai_answer(visit_id)
        if visit_ai is None:
            return None
        symptom = self._store.stored_symptom_check(visit_id)
        visit = self._store.stored_visit(visit_id)
        built = build_ai_input(json.loads(visit.raw))
        tier = decide_tier(built.rows, len(built.descriptive_rows))
        lane = lane_for_tier(tier)
        patient_data = build_symptom_ai_input(json.loads(symptom.raw), built.ai_json, visit_ai)
        retrieved = select_retrieved_tests(built.rows, self._catalogue.parameter_categories(),
                                           self._catalogue.tests_by_category(), built.performed_test_names)
        if retrieved:
            patient_data["retrieved_tests"] = retrieved
        system_prompt, output_schema = self._prompts[SOURCE]
        model_input = build_model_input(system_prompt=system_prompt, output_schema=output_schema,
                                        processed=patient_data)
        path = processed_path(SOURCE, lane, blob_day(symptom.received_at), visit.idempotency_key)
        read_from = write_model_input(self._blobs, path, model_input)
        self._store.save_symptom_processed(visit_id, model_input, read_from)
        return Processed(visit_id, tier, lane, read_from)


def process_waiting_symptoms(store: VisitStore, processor: SymptomProcessor, now: datetime) -> list[Processed]:
    """The 5-minute timer calls this: process every symptom check that was cut short by a restart, or that was waiting
    for its visit's result. A check whose visit still has no result stays as it is."""
    waiting = store.symptoms_waiting_for_processing(now - STUCK_AFTER)
    return [done for visit_id in waiting if (done := processor.process(visit_id)) is not None]
