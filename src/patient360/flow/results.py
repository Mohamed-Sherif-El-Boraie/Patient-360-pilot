"""Receiving the AWS answers the Function app posts: save each one, build the Backend result, hand it to the sender."""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC
from typing import Any, Literal

import json_repair
import structlog
from pydantic import ValidationError

from patient360.domain.builders.ai_input import SOURCE_NAMES, build_ai_input
from patient360.domain.builders.backend_output import build_backend_output, build_failed_output, for_wrapper
from patient360.domain.builders.blocked import blocked_ai, is_blocked
from patient360.domain.common.blob_paths import MergedOutput, backend_output_path, parse_output_path
from patient360.domain.payload.ai_answer import AiAnswer
from patient360.domain.payload.values import as_whole_number
from patient360.domain.rules.classify import decide_tier, symptom_checker_open
from patient360.domain.rules.packages import normalize_name, package_entries, select_packages
from patient360.ports import BlobStore, BlobUnavailable, Catalogue, StoredVisit, VisitStore

log = structlog.get_logger()

SYMPTOM_CHECKER = "symptom-checker"
SYMPTOM_CHECKER_ID = 3
Analysis = Literal["visit", "symptom"]
Outcome = Literal["saved", "blocked", "duplicate", "unknown_visit", "source_mismatch", "invalid_output", "ignored",
                 "failed_recorded"]


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
    def __init__(self, store: VisitStore, catalogue: Catalogue, outputs: Mapping[str, BlobStore] | None = None) -> None:
        """``outputs``: the output container of each source, to keep the Backend body (``06-backend-output.json``)."""
        self._store, self._catalogue, self._outputs = store, catalogue, outputs or {}

    def receive_record(self, source: str, path: str, text: str) -> list[Received]:
        """Handle one record the Function app sent: a visit file, or one line of a batch JSONL (``path`` is the file).
        A path that is not an answer (the other phase files, a batch's per-visit copies) is ignored. A database
        error is not caught: the Function sends the record again."""
        found = parse_output_path(source, path)
        if found is None or found.kind == "batch_visit":
            log.info("output_ignored", source=source, status="ignored")
            return [Received(None, "ignored")]
        if found.kind == "failed":      # the body is not read: its shape is unknown and may be broken
            return [self._handle_failed(found)]
        result = self._handle_record(found, text)
        if found.kind == "batch_jsonl" and found.batch_id:
            # records of one batch arrive one by one: it is complete only when every visit has its output
            self._store.set_batch_status(found.batch_id, "completed" if self._store.batch_fully_answered(
                found.batch_id) else "partially_completed")
        return [result]

    def _handle_failed(self, found: MergedOutput) -> Received:
        """AWS wrote ``07-failed.json``: record the failure and queue the "failed" message for the Backend."""
        stored = self._store.find_visit(found.key) if found.key else None
        if stored is None:
            return self._ignore(found, found.key, "unknown_visit")
        analysis = self._analysis(found)
        if analysis == "visit" and SOURCE_NAMES[stored.source_id] != found.source:
            return self._ignore(found, found.key, "source_mismatch")
        if self._store.has_output(stored.visit_id, analysis):
            return Received(found.key, "duplicate")
        self._fail(found, stored, analysis, "aws_failed")
        return Received(found.key, "failed_recorded")

    def _fail(self, found: MergedOutput, stored: StoredVisit, analysis: Analysis, reason: str) -> None:
        """One transaction: the visit failed, and the normal delivery queue tells the Backend (``success`` false)."""
        failed = build_failed_output(
            patient_id=json.loads(stored.raw)["patient_id"], visit_id=stored.visit_id,
            source=SYMPTOM_CHECKER_ID if analysis == "symptom" else stored.source_id)
        self._store.save_failed_output(stored.visit_id, failed, analysis)
        self._keep_backend_output(found, stored, failed)
        log.warning(reason, source=found.source, status="failed")

    @staticmethod
    def _analysis(found: MergedOutput) -> Analysis:
        return "symptom" if found.source == SYMPTOM_CHECKER else "visit"

    def _handle_record(self, found: MergedOutput, text: str) -> Received:
        try:
            record = _loads(text)
            record_id = record["recordId"]
        except (ValueError, KeyError):
            return self._ignore(found, None, "invalid_output")
        stored = self._store.find_visit(record_id) if isinstance(record_id, str) else None
        if stored is None:
            return self._ignore(found, record_id, "unknown_visit")
        analysis = self._analysis(found)
        if analysis == "visit" and SOURCE_NAMES[stored.source_id] != found.source:
            return self._ignore(found, record_id, "source_mismatch")     # a symptom answer has the visit's key
        if self._store.has_output(stored.visit_id, analysis):
            return Received(record_id, "duplicate")
        if is_blocked(record):      # never read the model's text: the Backend gets the fixed message
            self._save(found, stored, record, blocked_ai(record), analysis, blocked=True, text=text)
            return Received(record_id, "blocked")
        try:
            ai = AiAnswer.model_validate(_loads(record["content"][0]["text"])).for_backend()
            if analysis == "visit" and not (ai.get("summary") and ai.get("simple_summary")):
                raise ValueError("a visit answer needs summary and simple_summary")     # a symptom check may skip them
        except (ValidationError, ValueError, KeyError, IndexError, TypeError):
            self._fail(found, stored, analysis, "invalid_ai_output")
            return Received(record_id, "invalid_output")
        self._save(found, stored, record, ai, analysis, text=text)
        return Received(record_id, "saved")

    def _save(self, found: MergedOutput, stored: StoredVisit, record: dict[str, Any],
              ai: dict[str, Any], analysis: Analysis, blocked: bool = False, text: str = "") -> None:
        public = self._public_output(stored, ai, analysis, blocked)
        self._store.save_output(stored.visit_id, record, public, analysis, internal_raw=text.encode() or None)
        self._keep_backend_output(found, stored, public)
        log.info("output_saved", source=found.source, stage="post_processing")

    def _keep_backend_output(self, found: MergedOutput, stored: StoredVisit, body: dict[str, Any]) -> None:
        """Keep the body we send to the Backend next to the answer (``06-backend-output.json``). The database is the
        record, so a Blob problem is only logged."""
        if (store := self._outputs.get(found.source)) is None:
            return
        try:
            store.put(backend_output_path(found.lane, found.day, stored.idempotency_key),
                      json.dumps(for_wrapper(body), ensure_ascii=False).encode())
        except BlobUnavailable:
            log.warning("backend_output_not_kept", source=found.source)

    def _ignore(self, found: MergedOutput, record_id: object, outcome: Outcome) -> Received:
        """A record that belongs to no visit of ours: only a log line. The file is still in Blob 2 where AWS put it."""
        log.warning("output_ignored", source=found.source, status=outcome)
        return Received(record_id if isinstance(record_id, str) else None, outcome)

    def _public_output(self, stored: StoredVisit, ai: dict[str, Any], analysis: Analysis,
                       blocked: bool) -> dict[str, Any]:
        """The symptom result is the visit result with the new answer: source 3, and each test's reason in the html.
        ``symptom_flag``: open for a visit that is not fully normal and not blocked; a symptom result is the symptom
        check itself, so it is always true (the visits column is set by the visit result only)."""
        raw = json.loads(stored.raw)
        built = build_ai_input(raw)
        symptom = analysis == "symptom"
        tier = decide_tier(built.rows, len(built.descriptive_rows))
        return build_backend_output(
            patient_id=raw["patient_id"], visit_id=stored.visit_id,
            source=SYMPTOM_CHECKER_ID if symptom else stored.source_id, rows=built.rows,
            ai=ai, packages=None if blocked else self._packages(raw["patient"], ai),
            arabic_test_names=self._catalogue.test_names_ar(),
            html_reasons=symptom, symptom_flag=symptom or symptom_checker_open(tier, blocked),
            html_symptom=self._symptom_for_html(stored.visit_id) if symptom else None)

    def _symptom_for_html(self, visit_id: int) -> tuple[str, str] | None:
        """The patient's symptom text and the UTC date (yyyy-MM-dd) it was received, for the html message; None if the
        stored check has no text (the visit wording is used)."""
        check = self._store.stored_symptom_check(visit_id)
        text = json.loads(check.raw).get("patient_symptoms")
        return (text, check.received_at.astimezone(UTC).date().isoformat()) if text else None

    def _packages(self, patient: dict[str, Any], ai: dict[str, Any]) -> list[dict[str, Any]]:
        """The packages for this patient (sex and age come from the visit we saved) and the tests the AI recommended.
        A test the symptom check removed is not asked for. A blocked answer gets no packages."""
        by_name = self._catalogue.test_ids_by_name()
        asked = [r["test_name"] for r in ai.get("recommendations") or []
                 if str((r.get("change_type") or {}).get("en", "")).strip().lower() != "removed"]
        requested = {by_name[key] for name in asked if (key := normalize_name(name)) in by_name}
        chosen = select_packages(self._catalogue.packages(), requested, as_whole_number(patient["age_days"]),
                                 as_whole_number(patient["sex"]))
        return package_entries(chosen, self._catalogue.test_names())
