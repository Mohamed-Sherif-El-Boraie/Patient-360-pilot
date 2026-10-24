"""The AI result of a visit or symptom check: saving it, and the queue that delivers it to the Backend."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from patient360.adapters.postgres.db import (
    CLAIM_TIMEOUT,
    Analysis,
    Database,
    current_payload,
    insert_payload,
    record_attempt,
    set_stage,
    source_priority_sql,
)
from patient360.domain.builders.blocked import is_blocked_ai
from patient360.ports import DeliveryUnit

BATCH_SOURCE_ID = 2
ADVISORY_LEVELS = {"none", "discuss_with_doctor", "seek_care_promptly"}
CHANGE_TYPES = {"kept", "added", "removed"}
_SYMPTOM_SOURCE = "symptom-checker"

# the source name and the received time of the unit, whichever analysis the row is for
_UNIT_SOURCE = f"(CASE WHEN rp.analysis = 'symptom' THEN '{_SYMPTOM_SOURCE}' ELSE s.source_name END)"
_UNIT_RECEIVED = "(CASE WHEN rp.analysis = 'symptom' THEN v.symptom_received_at ELSE v.created_at END)"


def _stage(name: str, analysis: Analysis) -> str:
    return f"{name}_symptom" if analysis == "symptom" else name


def _text(value: dict[str, Any] | None, language: str) -> str | None:
    return value.get(language) if value else None


def _allowed(value: dict[str, Any] | None, allowed: set[str]) -> dict[str, Any] | None:
    """The {en, ar} value when its English text is one of the ``allowed`` words (any case), else ``None``."""
    return value if value and str(value.get("en", "")).strip().lower() in allowed else None


class OutputsMixin:
    _db: Database

    def has_output(self, visit_id: int, analysis: Analysis = "visit") -> bool:
        with self._db.transaction() as conn:
            return conn.execute("SELECT 1 FROM ai_outputs WHERE visit_id = %s AND analysis = %s",
                                (visit_id, analysis)).fetchone() is not None

    def save_output(self, visit_id: int, internal_output: dict[str, Any], public_output: dict[str, Any],
                    analysis: Analysis = "visit") -> None:
        ai = public_output["ai"]
        blocked = is_blocked_ai(ai)
        status = "guardrail_blocked" if blocked else "answered" if analysis == "symptom" else "success"
        html = public_output.get("html") or {}
        with self._db.transaction() as conn:
            source_id = conn.execute("SELECT source_id FROM visits WHERE visit_id = %s", (visit_id,)).fetchone()[
                "source_id"]
            # a second save replaces the answer rows (the children go with it); the payload history stays
            conn.execute("DELETE FROM ai_outputs WHERE visit_id = %s AND analysis = %s", (visit_id, analysis))
            conn.execute(
                "INSERT INTO ai_outputs (visit_id, analysis, status, summary_en, summary_ar, simple_summary_en, "
                "simple_summary_ar, note_en, note_ar, html_en, html_ar, generated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())",
                (visit_id, analysis, status, _text(ai.get("summary"), "en"), _text(ai.get("summary"), "ar"),
                 _text(ai.get("simple_summary"), "en"), _text(ai.get("simple_summary"), "ar"),
                 _text(ai.get("note"), "en"), _text(ai.get("note"), "ar"), html.get("en"), html.get("ar")))
            for alert in ai.get("critical_alerts") or []:
                conn.execute("INSERT INTO ai_critical_alerts (visit_id, analysis, alert_en, alert_ar) "
                             "VALUES (%s, %s, %s, %s)", (visit_id, analysis, alert["en"], alert["ar"]))
            for recommendation in ai.get("recommendations") or []:
                self._save_recommendation(conn, visit_id, analysis, recommendation)
            mode = "batch_inference" if analysis == "visit" and source_id == BATCH_SOURCE_ID else "on_demand"
            self._save_model_calls(conn, visit_id, analysis, internal_output, mode)
            insert_payload(conn, visit_id, analysis, "output_internal", internal_output)
            insert_payload(conn, visit_id, analysis, "output_public", public_output)
            conn.execute(
                "INSERT INTO record_processing (visit_id, analysis, step, read_from, status) "
                "VALUES (%s, %s, 'deliver', 'postgresql', 'pending') "
                "ON CONFLICT (visit_id, analysis) DO UPDATE SET step = 'deliver', status = 'pending', attempts = 0, "
                "next_retry_at = NULL, last_error = NULL, claimed_by = NULL, updated_at = now()",
                (visit_id, analysis))
            set_stage(conn, visit_id, _stage("post_processing", analysis))

    @staticmethod
    def _save_recommendation(conn, visit_id: int, analysis: Analysis, rec: dict[str, Any]) -> None:
        """One recommendation row. A test we do not know is skipped: its text is still in the saved payload."""
        test = conn.execute(
            "SELECT test_id FROM tests WHERE lower(regexp_replace(btrim(test_name_en), '\\s+', ' ', 'g')) = %s "
            "ORDER BY active DESC, test_id LIMIT 1", (" ".join(rec["test_name"].lower().split()),)).fetchone()
        if test is None:
            return
        priority = rec["priority"]
        urgent_or_important = str(priority.get("en", "")).strip().lower() in ("urgent", "important")
        level = _allowed((rec.get("advisory") or {}).get("level"), ADVISORY_LEVELS)
        change = _allowed(rec.get("change_type"), CHANGE_TYPES)
        advisory = rec.get("advisory") or {}
        conn.execute(
            "INSERT INTO ai_recommendations (visit_id, analysis, test_id, priority, priority_ar, change_type, "
            "change_type_ar, advisory_level, advisory_level_ar, advisory_message_en, advisory_message_ar, reason_en, "
            "reason_ar, explanation_en, explanation_ar, timing_en, timing_ar, guideline_en, guideline_ar) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (visit_id, analysis, test["test_id"],
             priority["en"].strip().lower() if urgent_or_important else "important", priority.get("ar"),
             change["en"].strip().lower() if change else None, change["ar"] if change else None,
             level["en"].strip().lower() if level else None, level["ar"] if level else None,
             _text(advisory.get("message"), "en"), _text(advisory.get("message"), "ar"),
             rec["reason"]["en"], rec["reason"]["ar"],
             _text(rec.get("explanation"), "en"), _text(rec.get("explanation"), "ar"),
             rec["timing"]["en"], rec["timing"]["ar"], rec["guideline"]["en"], rec["guideline"]["ar"]))

    @staticmethod
    def _save_model_calls(conn, visit_id: int, analysis: Analysis, internal: dict[str, Any], mode: str) -> None:
        """The analysis call (metadata.en) and the translation call (metadata.ar, when there is one)."""
        model = internal.get("model")
        if not model:
            return
        metadata = internal.get("metadata") or {}
        for purpose, language in (("analysis", "en"), ("translation", "ar")):
            call = metadata.get(language)
            if call is None and purpose == "translation":
                continue
            call = call or {}
            usage = call.get("usage") or {}
            conn.execute(
                "INSERT INTO ai_model_calls (visit_id, analysis, purpose, model_id, mode, input_tokens, output_tokens, "
                "cache_creation_input_tokens, cache_read_input_tokens, stop_reason, stop_sequence) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (visit_id, analysis, purpose, model, mode, usage.get("input_tokens"), usage.get("output_tokens"),
                 usage.get("cache_creation_input_tokens"), usage.get("cache_read_input_tokens"),
                 call.get("stop_reason"), call.get("stop_sequence")))

    def mark_output_failed(self, visit_id: int, error_type: str, analysis: Analysis = "visit") -> None:
        with self._db.transaction() as conn:
            conn.execute("INSERT INTO ai_outputs (visit_id, analysis, status) VALUES (%s, %s, 'error') "
                         "ON CONFLICT DO NOTHING", (visit_id, analysis))
            conn.execute("UPDATE record_processing SET status = 'failed', last_error = %s, updated_at = now() "
                         "WHERE visit_id = %s AND analysis = %s", (error_type, visit_id, analysis))
            set_stage(conn, visit_id, _stage("failed", analysis))

    def visit_ai_answer(self, visit_id: int) -> dict[str, Any] | None:
        with self._db.transaction() as conn:
            public = current_payload(conn, visit_id, "visit", "output_public")
        return public["ai"] if public else None

    def set_batch_status(self, batch_id: str, status: str) -> None:
        with self._db.transaction() as conn:
            conn.execute("UPDATE batches SET status = %s WHERE batch_id = %s", (status, batch_id))

    def claim_next_delivery(self, now: datetime) -> DeliveryUnit | None:
        with self._db.transaction() as conn:
            row = conn.execute(
                f"SELECT rp.visit_id, rp.analysis, rp.attempts, {_UNIT_SOURCE} AS source "
                "FROM record_processing rp JOIN visits v ON v.visit_id = rp.visit_id "
                "JOIN sources s ON s.source_id = v.source_id "
                "WHERE rp.step = 'deliver' AND ((rp.status = 'pending' AND "
                "(rp.next_retry_at IS NULL OR rp.next_retry_at <= %(now)s)) "
                "OR (rp.status = 'processing' AND rp.updated_at < %(now)s - %(timeout)s)) "
                f"ORDER BY {source_priority_sql(_UNIT_SOURCE)}, coalesce({_UNIT_RECEIVED}, v.created_at), rp.visit_id "
                "LIMIT 1 FOR UPDATE OF rp SKIP LOCKED", {"now": now, "timeout": CLAIM_TIMEOUT}).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE record_processing SET status = 'processing', claimed_by = %s, updated_at = %s "
                         "WHERE visit_id = %s AND analysis = %s",
                         (row["source"], now, row["visit_id"], row["analysis"]))
            return DeliveryUnit(row["visit_id"], row["source"], row["attempts"], row["analysis"])

    def public_output_of(self, unit: DeliveryUnit) -> dict[str, Any]:
        with self._db.transaction() as conn:
            body = current_payload(conn, unit.visit_id, unit.analysis, "output_public")
        if body is None:
            raise LookupError(f"no public output for visit {unit.visit_id} ({unit.analysis})")
        return body

    def mark_delivered(self, unit: DeliveryUnit) -> None:
        with self._db.transaction() as conn:
            row = conn.execute(
                "UPDATE record_processing SET status = 'delivered', updated_at = now() "
                "WHERE visit_id = %s AND analysis = %s RETURNING attempts", (unit.visit_id, unit.analysis)).fetchone()
            record_attempt(conn, unit.visit_id, unit.analysis, row["attempts"] + 1, "sent")
            set_stage(conn, unit.visit_id, _stage("delivered", unit.analysis))

    def mark_delivery_failed_attempt(self, unit: DeliveryUnit, error_type: str, retry_at: datetime | None) -> None:
        with self._db.transaction() as conn:
            row = conn.execute(
                "UPDATE record_processing SET attempts = attempts + 1, last_error = %s, claimed_by = NULL, "
                "status = %s, next_retry_at = %s, updated_at = now() WHERE visit_id = %s AND analysis = %s "
                "RETURNING attempts", (error_type, "failed" if retry_at is None else "pending", retry_at,
                                       unit.visit_id, unit.analysis)).fetchone()
            record_attempt(conn, unit.visit_id, unit.analysis, row["attempts"],
                           "failed" if retry_at is None else "retry", error_type)
            if retry_at is None:
                set_stage(conn, unit.visit_id, _stage("failed", unit.analysis))
