"""The upload work queue: save the processed request, make batches, claim the next unit, record how the upload went.

Claims depend only on ``record_processing`` (and ``batches``); ``visits.stage`` is kept up to date for people to read.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

import psycopg

from patient360.adapters.postgres.db import (
    CLAIM_TIMEOUT,
    Database,
    current_payload,
    insert_payload,
    record_attempt,
    set_stage,
    source_priority_sql,
)
from patient360.domain.common.ids import format_batch_id
from patient360.ports import BatchVisit, ClaimedBatch, WorkUnit

Conn = psycopg.Connection[dict[str, Any]]

_BATCH_SEQUENCE = {"normal": "batch_normal_seq", "critical": "batch_critical_seq"}
_SOURCE = "CASE rp.analysis WHEN 'symptom' THEN 'symptom-checker' ELSE s.source_name END"
_CLAIMABLE = ("(rp.status = 'pending' AND (rp.next_retry_at IS NULL OR rp.next_retry_at <= %(now)s) "
              "OR rp.status = 'processing' AND rp.updated_at < %(stale)s)")


def _suffix(analysis: str) -> str:
    return "_symptom" if analysis == "symptom" else ""


class WorkMixin:
    _db: Database

    # ---- saving the processed request -----------------------------------------------------------
    @staticmethod
    def _save_processed(conn: Conn, visit_id: int, analysis: str, model_input: dict[str, Any], read_from: str) -> None:
        insert_payload(conn, visit_id, analysis, "processed", model_input)
        conn.execute(
            "INSERT INTO record_processing (visit_id, analysis, step, read_from, status, attempts, next_retry_at, "
            "last_error, claimed_by, updated_at) VALUES (%s, %s, 'upload', %s, 'pending', 0, NULL, NULL, NULL, now()) "
            "ON CONFLICT (visit_id, analysis) DO UPDATE SET step = 'upload', read_from = excluded.read_from, "
            "status = 'pending', attempts = 0, next_retry_at = NULL, last_error = NULL, claimed_by = NULL, "
            "updated_at = now()", (visit_id, analysis, read_from))
        set_stage(conn, visit_id, "processed" + _suffix(analysis))

    def save_processed(self, visit_id: int, tier: str, model_input: dict[str, Any],
                       read_from: Literal["blob", "postgresql"]) -> None:
        with self._db.transaction() as conn:
            conn.execute("UPDATE visits SET tier = %s WHERE visit_id = %s", (tier, visit_id))
            self._save_processed(conn, visit_id, "visit", model_input, read_from)

    def save_symptom_processed(self, visit_id: int, model_input: dict[str, Any],
                               read_from: Literal["blob", "postgresql"]) -> None:
        with self._db.transaction() as conn:
            self._save_processed(conn, visit_id, "symptom", model_input, read_from)

    # ---- batches --------------------------------------------------------------------------------
    @staticmethod
    def _batch_visits(conn: Conn, batch_id: str) -> list[BatchVisit]:
        rows = conn.execute(
            "SELECT v.idempotency_key, p.body FROM visits v "
            "JOIN record_payloads_current p ON p.visit_id = v.visit_id AND p.analysis = 'visit' "
            "AND p.kind = 'processed' "
            "WHERE v.batch_id = %s ORDER BY v.created_at, v.visit_id", (batch_id,)).fetchall()
        return [BatchVisit(row["idempotency_key"], row["body"]) for row in rows]

    def claim_batch(self, lane: Literal["normal", "critical"], size: int) -> ClaimedBatch | None:
        sequence = _BATCH_SEQUENCE[lane]
        with self._db.transaction() as conn:
            waiting = conn.execute(
                "SELECT v.visit_id FROM visits v JOIN record_processing rp ON rp.visit_id = v.visit_id "
                "AND rp.analysis = 'visit' AND rp.step = 'upload' AND rp.status = 'pending' "
                "WHERE v.source_id = 2 AND v.batch_id IS NULL AND v.lane = %s "
                "ORDER BY v.created_at, v.visit_id LIMIT %s FOR UPDATE OF v SKIP LOCKED", (lane, size)).fetchall()
            if len(waiting) < size:
                return None
            number = conn.execute("SELECT nextval(%s::regclass) AS n", (sequence,)).fetchone()["n"]
            batch_id = format_batch_id(lane, number)
            created_at = conn.execute("INSERT INTO batches (batch_id) VALUES (%s) RETURNING created_at",
                                      (batch_id,)).fetchone()["created_at"]
            visit_ids = [row["visit_id"] for row in waiting]
            conn.execute("UPDATE visits SET batch_id = %s, stage = 'queued', updated_at = now() "
                         "WHERE visit_id = ANY(%s)", (batch_id, visit_ids))
            return ClaimedBatch(batch_id, self._batch_visits(conn, batch_id), created_at)

    def set_batch_read_from(self, batch_id: str, read_from: Literal["blob", "postgresql"]) -> None:
        with self._db.transaction() as conn:
            conn.execute("UPDATE batches SET read_from = %s WHERE batch_id = %s", (read_from, batch_id))

    def batch_model_inputs(self, batch_id: str) -> list[BatchVisit]:
        with self._db.transaction() as conn:
            return self._batch_visits(conn, batch_id)

    # ---- claiming -------------------------------------------------------------------------------
    def claim_next_work(self, now: datetime) -> WorkUnit | None:
        with self._db.transaction() as conn:
            unit = self._claim_visit_unit(conn, now) or self._claim_batch_unit(conn, now)
        return unit

    @staticmethod
    def _claim_visit_unit(conn: Conn, now: datetime) -> WorkUnit | None:
        row = conn.execute(
            f"SELECT rp.visit_id, rp.analysis, rp.read_from, rp.attempts, v.idempotency_key, v.lane, "
            f"{_SOURCE} AS source, "
            f"CASE rp.analysis WHEN 'symptom' THEN v.symptom_received_at ELSE v.created_at END AS received_at "
            f"FROM record_processing rp JOIN visits v USING (visit_id) JOIN sources s USING (source_id) "
            f"WHERE rp.step = 'upload' AND {_CLAIMABLE} "
            f"AND (rp.analysis = 'symptom' OR (v.source_id <> 2 AND v.batch_id IS NULL)) "
            f"ORDER BY {source_priority_sql('(' + _SOURCE + ')')}, received_at, rp.visit_id "
            f"LIMIT 1 FOR UPDATE OF rp SKIP LOCKED", {"now": now, "stale": now - CLAIM_TIMEOUT}).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE record_processing SET status = 'processing', claimed_by = %s, updated_at = %s "
                     "WHERE visit_id = %s AND analysis = %s", (row["source"], now, row["visit_id"], row["analysis"]))
        set_stage(conn, row["visit_id"], "queued" + _suffix(row["analysis"]))
        return WorkUnit(row["source"], row["lane"], row["received_at"], row["read_from"], row["attempts"],
                        visit_id=row["visit_id"], idempotency_key=row["idempotency_key"], analysis=row["analysis"])

    @staticmethod
    def _claim_batch_unit(conn: Conn, now: datetime) -> WorkUnit | None:
        row = conn.execute(
            "SELECT batch_id, read_from, attempts, created_at FROM batches WHERE status = 'created' "
            "AND (claimed_at IS NULL OR claimed_at < %s) AND (next_retry_at IS NULL OR next_retry_at <= %s) "
            "ORDER BY created_at, batch_id LIMIT 1 FOR UPDATE SKIP LOCKED", (now - CLAIM_TIMEOUT, now)).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE batches SET claimed_at = %s WHERE batch_id = %s", (now, row["batch_id"]))
        return WorkUnit("batch", row["batch_id"].split("_")[0], row["created_at"], row["read_from"], row["attempts"],
                        batch_id=row["batch_id"])

    def model_input_of(self, unit: WorkUnit) -> dict[str, Any]:
        with self._db.transaction() as conn:
            body = current_payload(conn, unit.visit_id, unit.analysis, "processed")
        if body is None:
            raise LookupError(f"visit {unit.visit_id} has no processed {unit.analysis} request")
        return body

    # ---- the result of an upload ----------------------------------------------------------------
    @staticmethod
    def _batch_visit_ids(conn: Conn, batch_id: str) -> list[int]:
        rows = conn.execute("SELECT visit_id FROM visits WHERE batch_id = %s ORDER BY created_at, visit_id",
                            (batch_id,)).fetchall()
        return [row["visit_id"] for row in rows]

    def mark_sent(self, unit: WorkUnit) -> None:
        with self._db.transaction() as conn:
            if unit.batch_id:
                attempts = conn.execute(
                    "UPDATE batches SET status = 'submitted', claimed_at = NULL WHERE batch_id = %s "
                    "RETURNING attempts", (unit.batch_id,)).fetchone()["attempts"]
                visit_ids = self._batch_visit_ids(conn, unit.batch_id)
                conn.execute("UPDATE record_processing SET status = 'sent', updated_at = now() "
                             "WHERE analysis = 'visit' AND visit_id = ANY(%s)", (visit_ids,))
                for visit_id in visit_ids:
                    record_attempt(conn, visit_id, "visit", attempts + 1, "sent")
                    set_stage(conn, visit_id, "sent_to_aws")
                return
            attempts = conn.execute(
                "UPDATE record_processing SET status = 'sent', updated_at = now() WHERE visit_id = %s "
                "AND analysis = %s RETURNING attempts", (unit.visit_id, unit.analysis)).fetchone()["attempts"]
            record_attempt(conn, unit.visit_id, unit.analysis, attempts + 1, "sent")
            set_stage(conn, unit.visit_id, "sent_to_aws" + _suffix(unit.analysis))

    def mark_failed_attempt(self, unit: WorkUnit, error_type: str, retry_at: datetime | None) -> None:
        outcome = "failed" if retry_at is None else "retry"
        with self._db.transaction() as conn:
            if unit.batch_id:
                attempts = conn.execute(
                    "UPDATE batches SET attempts = attempts + 1, last_error = %s, next_retry_at = %s, "
                    "claimed_at = NULL, status = CASE WHEN %s THEN 'failed' ELSE status END WHERE batch_id = %s "
                    "RETURNING attempts",
                    (error_type, retry_at, retry_at is None, unit.batch_id)).fetchone()["attempts"]
                for visit_id in self._batch_visit_ids(conn, unit.batch_id):
                    record_attempt(conn, visit_id, "visit", attempts, outcome, error_type)
                    if retry_at is None:
                        set_stage(conn, visit_id, "failed")
                return
            attempts = conn.execute(
                "UPDATE record_processing SET attempts = attempts + 1, last_error = %s, claimed_by = NULL, "
                "next_retry_at = %s, status = %s, updated_at = now() WHERE visit_id = %s AND analysis = %s "
                "RETURNING attempts",
                (error_type, retry_at, "failed" if retry_at is None else "pending", unit.visit_id,
                 unit.analysis)).fetchone()["attempts"]
            record_attempt(conn, unit.visit_id, unit.analysis, attempts, outcome, error_type)
            if retry_at is None:
                set_stage(conn, unit.visit_id, "failed" + _suffix(unit.analysis))

