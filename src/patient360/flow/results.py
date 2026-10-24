"""Receiving what AWS wrote to Azure Blob 2: save it, build the Backend result, hand it to the sender."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

import structlog
from pydantic import ValidationError

from patient360.domain.builders.ai_input import build_ai_input
from patient360.domain.builders.backend_output import build_backend_output
from patient360.domain.common.blob_paths import MergedOutput, merged_output_path, parse_merged_output_path
from patient360.domain.payload.ai_answer import AiAnswer
from patient360.ports import BlobStore, Catalogue, StoredVisit, VisitStore

log = structlog.get_logger()


@dataclass(frozen=True)
class Received:
    record_id: str | None
    outcome: Literal["saved", "duplicate", "unknown_visit", "invalid_output"]


class OutputReceiver:
    def __init__(self, store: VisitStore, output_blobs: BlobStore, catalogue: Catalogue) -> None:
        self._store, self._blobs, self._catalogue = store, output_blobs, catalogue

    def receive(self, path: str) -> list[Received]:
        """Handle one new file of Blob 2. A visit file holds one result, a batch JSONL holds one per line.
        Files that are not a result (our own split files, anything else) are ignored."""
        found = parse_merged_output_path(path)
        if found is None or found.kind == "batch_visit" or found.source == "symptom-checker":
            return []      # a symptom-check answer has its own flow (Task 7)
        data = self._blobs.get(path)      # BlobUnavailable is not caught: the event is tried again later
        if data is None:
            return []
        lines = data.decode().splitlines() if found.kind == "batch_jsonl" else [data.decode()]
        results = [self._handle_record(found, line) for line in lines if line.strip()]
        if found.batch_id:
            self._store.set_batch_status(found.batch_id, "completed" if all(
                r.outcome in ("saved", "duplicate") for r in results) else "partially_completed")
        return results

    def _handle_record(self, found: MergedOutput, text: str) -> Received:
        try:
            record = json.loads(text)
            record_id = record["recordId"]
        except (ValueError, KeyError, TypeError):
            return Received(None, "invalid_output")
        stored = self._store.find_visit(record_id) if isinstance(record_id, str) else None
        if stored is None:
            log.warning("output_for_unknown_visit", source=found.source)
            return Received(record_id if isinstance(record_id, str) else None, "unknown_visit")
        if self._store.has_output(stored.visit_id):
            return Received(record_id, "duplicate")
        try:
            ai = AiAnswer.model_validate_json(record["content"][0]["text"]).for_backend()
        except (ValidationError, KeyError, IndexError, TypeError):
            self._store.mark_output_failed(stored.visit_id, "invalid_ai_output")
            log.warning("invalid_ai_output", source=found.source, status="failed")
            return Received(record_id, "invalid_output")
        if found.batch_id:      # a batch result also gets its own file, so the folder tree is complete
            split_path = merged_output_path("batch", found.lane, found.day, record_id, found.batch_id)
            self._blobs.put(split_path, text.encode())
        self._store.save_output(stored.visit_id, record, self._public_output(stored, ai))
        log.info("output_saved", source=found.source, stage="post_processing")
        return Received(record_id, "saved")

    def _public_output(self, stored: StoredVisit, ai: dict[str, Any]) -> dict[str, Any]:
        raw = json.loads(stored.raw)
        built = build_ai_input(raw)
        return build_backend_output(
            patient_id=raw["patient_id"], visit_id=stored.visit_id, source=stored.source_id, rows=built.rows,
            descriptive_rows=built.descriptive_rows, ai=ai, arabic_test_names=self._catalogue.test_names_ar())
