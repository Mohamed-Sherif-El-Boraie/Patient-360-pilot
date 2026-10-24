"""Receiving what AWS wrote to Azure Blob 2: save it, build the Backend result, hand it to the sender."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

import json_repair
import structlog
from pydantic import ValidationError

from patient360.domain.builders.ai_input import SOURCE_NAMES, build_ai_input
from patient360.domain.builders.backend_output import build_backend_output
from patient360.domain.builders.blocked import blocked_ai, is_blocked
from patient360.domain.common.blob_paths import MergedOutput, merged_output_path, parse_merged_output_path
from patient360.domain.payload.ai_answer import AiAnswer
from patient360.ports import BlobStore, Catalogue, StoredVisit, VisitStore

log = structlog.get_logger()

Outcome = Literal["saved", "blocked", "duplicate", "unknown_visit", "source_mismatch", "invalid_output"]


@dataclass(frozen=True)
class Received:
    record_id: str | None
    outcome: Outcome


def _loads(text: str) -> dict[str, Any]:
    """Strict JSON first. If that fails, the repair library gets one try; a result that is not an object is refused.
    A repaired record still has to pass the answer shape check before anything is saved."""
    try:
        value = json.loads(text)
    except ValueError:
        value = json_repair.loads(text)
        log.warning("output_repaired")
    if not isinstance(value, dict):
        raise ValueError("not a JSON object")
    return value


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
        text = data.decode(errors="replace")      # a broken byte becomes an odd character, never a crash
        lines = text.splitlines() if found.kind == "batch_jsonl" else [text]
        results = [self._handle_record(found, line) for line in lines if line.strip()]
        if found.batch_id:
            done = ("saved", "blocked", "duplicate")
            self._store.set_batch_status(found.batch_id, "completed" if all(
                r.outcome in done for r in results) else "partially_completed")
        return results

    def _handle_record(self, found: MergedOutput, text: str) -> Received:
        try:
            record = _loads(text)
            record_id = record["recordId"]
        except (ValueError, KeyError):
            return self._ignore(found, None, "invalid_output")
        stored = self._store.find_visit(record_id) if isinstance(record_id, str) else None
        if stored is None:
            return self._ignore(found, record_id, "unknown_visit")
        if SOURCE_NAMES[stored.source_id] != found.source:
            return self._ignore(found, record_id, "source_mismatch")
        if self._store.has_output(stored.visit_id):
            return Received(record_id, "duplicate")
        if is_blocked(record):      # never read the model's text: the Backend gets the fixed message
            self._save(found, text, stored, record, blocked_ai(record))
            return Received(record_id, "blocked")
        try:
            ai = AiAnswer.model_validate(_loads(record["content"][0]["text"])).for_backend()
        except (ValidationError, ValueError, KeyError, IndexError, TypeError):
            self._store.mark_output_failed(stored.visit_id, "invalid_ai_output")
            log.warning("invalid_ai_output", source=found.source, status="failed")
            return Received(record_id, "invalid_output")
        self._save(found, text, stored, record, ai)
        return Received(record_id, "saved")

    def _save(self, found: MergedOutput, text: str, stored: StoredVisit, record: dict[str, Any],
              ai: dict[str, Any]) -> None:
        if found.batch_id:      # a batch result also gets its own file, so the folder tree is complete
            split_path = merged_output_path("batch", found.lane, found.day, record["recordId"], found.batch_id)
            self._blobs.put(split_path, text.encode())
        self._store.save_output(stored.visit_id, record, self._public_output(stored, ai))
        log.info("output_saved", source=found.source, stage="post_processing")

    def _ignore(self, found: MergedOutput, record_id: object, outcome: Outcome) -> Received:
        """A record that belongs to no visit of ours: only a log line. The file is still in Blob 2 where AWS put it."""
        log.warning("output_ignored", source=found.source, status=outcome)
        return Received(record_id if isinstance(record_id, str) else None, outcome)

    def _public_output(self, stored: StoredVisit, ai: dict[str, Any]) -> dict[str, Any]:
        raw = json.loads(stored.raw)
        built = build_ai_input(raw)
        return build_backend_output(
            patient_id=raw["patient_id"], visit_id=stored.visit_id, source=stored.source_id, rows=built.rows,
            descriptive_rows=built.descriptive_rows, ai=ai, arabic_test_names=self._catalogue.test_names_ar())
