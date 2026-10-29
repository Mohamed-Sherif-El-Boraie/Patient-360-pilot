"""Receiving side of the store: visits, their symptom checks and rejected payloads."""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from psycopg.types.json import Jsonb

from patient360.adapters.postgres.db import Database, insert_payload, set_stage
from patient360.domain.builders.ai_input import build_ai_input
from patient360.domain.payload.values import html_to_plain_text
from patient360.domain.rules.logic import lab_flag_status
from patient360.ports import StoredSymptomCheck, StoredVisit


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


class VisitsMixin:
    _db: Database

    def visit_exists(self, visit_id: int) -> bool:
        with self._db.transaction() as conn:
            return conn.execute("SELECT 1 FROM visits WHERE visit_id = %s", (visit_id,)).fetchone() is not None

    def save_visit(self, payload: dict[str, Any], raw: bytes, idempotency_key: str, received_at: datetime,
                   read_from: Literal["blob", "postgresql"]) -> None:
        """Write everything the Backend sent in one transaction. ``read_from`` is not used: the raw bytes are always
        kept in PostgreSQL. An unknown company, test or parameter is added (tests and parameters inactive) instead of
        failing, so a new catalogue entry never loses a visit."""
        # Numbers parsed as Decimal keep their text as sent ("5.10" stays "5.10"); the AI input needs plain numbers.
        sent = json.loads(raw, parse_float=Decimal)
        ai = build_ai_input(json.loads(raw))
        statuses = {(r["test_id"], r["parameter_id"]): r for r in ai.rows}
        descriptive_text = {r["test_id"]: r["text_value"] for r in ai.descriptive_rows}
        visit_id = payload["visit_id"]
        with self._db.transaction() as conn:
            number = payload["company_id"]
            conn.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                         (number, f"Company {number}"))
            for test in sent["tests"]:
                conn.execute("INSERT INTO tests (test_id, test_name_en, active) VALUES (%s, %s, false) "
                             "ON CONFLICT DO NOTHING", (test["test_id"], test["test_name"]))
                for result in test["results"]:
                    conn.execute("INSERT INTO parameters (parameter_id, parameter_name, active) VALUES (%s, %s, false) "
                                 "ON CONFLICT DO NOTHING", (result["parameter_id"], result["parameter_name"]))
            for item in sent["descriptive_results"]:
                conn.execute("INSERT INTO tests (test_id, test_name_en, active) VALUES (%s, %s, false) "
                             "ON CONFLICT DO NOTHING", (item["test_id"], item["test_name"]))
            conn.execute(
                "INSERT INTO visits (visit_id, patient_id, idempotency_key, source_id, company_id, age_days, sex_id, "
                "created_at, stage) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'received')",
                (visit_id, payload["patient_id"], idempotency_key, payload["source"], number,
                 ai.ai_json["patient"]["age_days"], payload["patient"]["sex"], received_at))
            for test in sent["tests"]:
                test_id = test["test_id"]
                conn.execute("INSERT INTO visit_tests (visit_id, test_id, test_remark) VALUES (%s, %s, %s)",
                             (visit_id, test_id, test["test_remark"]))
                for result in test["results"]:
                    self._save_lab_result(conn, visit_id, number, test_id, result,
                                          statuses[(test_id, result["parameter_id"])])
            for item in sent["descriptive_results"]:
                test_id = item["test_id"]
                conn.execute("INSERT INTO descriptive_results (visit_id, test_id, result_date, text_value, status) "
                             "VALUES (%s, %s, %s, %s, 'text')", (visit_id, test_id, item["result_date"],
                                                                 descriptive_text[test_id]))
                for point in item.get("history") or []:
                    conn.execute(
                        "INSERT INTO descriptive_result_history (visit_id, test_id, company_id, history_visit_id, "
                        "history_date, history_value) VALUES (%s, %s, %s, %s, %s, %s)",
                        (visit_id, test_id, number, point["history_visit_id"], point["history_date"],
                         _text(html_to_plain_text(point["history_value"]))))      # HTML in, plain text stored
            for answer in sent["visit_answer_list"]:
                conn.execute("INSERT INTO visit_answers (visit_id, preparation_id, preparation_name, answer) "
                             "VALUES (%s, %s, %s, %s)",
                             (visit_id, answer["preparation_id"], answer["preparation_name"], answer["answer"]))
            insert_payload(conn, visit_id, "visit", "raw", json.loads(raw), raw_bytes=raw)

    @staticmethod
    def _save_lab_result(conn: Any, visit_id: int, company_id: int, test_id: int, result: dict[str, Any],
                         judged: dict[str, Any]) -> None:
        reference, critical = result["reference"], result["critical"] or {}
        flag = lab_flag_status(result["lab_flag"])
        conn.execute(
            "INSERT INTO lab_results (visit_id, test_id, parameter_id, result_date, value, unit, reference_low, "
            "reference_high, reference_range, lab_flag, critical_low, critical_high, critical_range_text, "
            "critical_direction, status, alert, parameter_remark) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (visit_id, test_id, result["parameter_id"], result["result_date"], _text(result["value"]), result["unit"],
             _text(reference["low"]), _text(reference["high"]), reference["range"], flag.capitalize() if flag else None,
             _text(critical.get("low")), _text(critical.get("high")), critical.get("range_text"),
             judged["critical_direction"], judged["status"], judged["alert"], result["parameter_remark"]))
        for point in result.get("history") or []:
            conn.execute(
                "INSERT INTO lab_result_history (visit_id, test_id, parameter_id, company_id, history_visit_id, "
                "history_date, history_value) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (visit_id, test_id, result["parameter_id"], company_id, point["history_visit_id"],
                 point["history_date"], _text(point["history_value"])))

    def _raw_of(self, conn: Any, visit_id: int, analysis: str) -> bytes | None:
        row = conn.execute("SELECT raw_bytes FROM record_payloads_current "
                           "WHERE visit_id = %s AND analysis = %s AND kind = 'raw'", (visit_id, analysis)).fetchone()
        return bytes(row["raw_bytes"]) if row else None

    def _stored_visit(self, conn: Any, row: dict[str, Any] | None) -> StoredVisit | None:
        if row is None:
            return None
        return StoredVisit(row["visit_id"], row["idempotency_key"], row["source_id"], row["created_at"],
                           self._raw_of(conn, row["visit_id"], "visit") or b"")

    def stored_visit(self, visit_id: int) -> StoredVisit:
        with self._db.transaction() as conn:
            row = conn.execute("SELECT visit_id, idempotency_key, source_id, created_at FROM visits "
                               "WHERE visit_id = %s", (visit_id,)).fetchone()
            if row is None:
                raise LookupError(f"visit {visit_id} not found")
            return self._stored_visit(conn, row)  # type: ignore[return-value]

    def find_visit(self, idempotency_key: str) -> StoredVisit | None:
        with self._db.transaction() as conn:
            row = conn.execute("SELECT visit_id, idempotency_key, source_id, created_at FROM visits "
                               "WHERE idempotency_key = %s", (idempotency_key,)).fetchone()
            return self._stored_visit(conn, row)

    def visits_waiting_for_processing(self, received_before: datetime) -> list[int]:
        """Visits received before the cutoff that have never been processed (no work row), oldest first."""
        with self._db.transaction() as conn:
            rows = conn.execute(
                "SELECT v.visit_id FROM visits v WHERE v.created_at < %s AND NOT EXISTS ("
                "SELECT 1 FROM record_processing p WHERE p.visit_id = v.visit_id AND p.analysis = 'visit') "
                "ORDER BY v.created_at, v.visit_id", (received_before,)).fetchall()
        return [r["visit_id"] for r in rows]

    def symptom_check_state(
        self, visit_id: int, patient_id: int,
    ) -> Literal["no_visit", "already_used", "closed", "ok"]:
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT patient_id, symptom_received_at, symptom_flag FROM visits WHERE visit_id = %s",
                (visit_id,)).fetchone()
        if row is None or row["patient_id"] != patient_id:
            return "no_visit"
        if row["symptom_received_at"] is not None:
            return "already_used"
        return "closed" if row["symptom_flag"] is False else "ok"       # no result yet (NULL): still open

    def save_symptom_check(self, payload: dict[str, Any], raw: bytes, received_at: datetime,
                           read_from: Literal["blob", "postgresql"]) -> None:
        """The symptom text, its arrival time, stage ``received_symptom`` and the raw bytes, in one transaction."""
        visit_id = payload["visit_id"]
        with self._db.transaction() as conn:
            conn.execute("UPDATE visits SET patient_symptoms = %s, symptom_received_at = %s WHERE visit_id = %s",
                         (payload["patient_symptoms"], received_at, visit_id))
            set_stage(conn, visit_id, "received_symptom")
            insert_payload(conn, visit_id, "symptom", "raw", json.loads(raw), raw_bytes=raw)

    def stored_symptom_check(self, visit_id: int) -> StoredSymptomCheck:
        with self._db.transaction() as conn:
            row = conn.execute("SELECT symptom_received_at FROM visits WHERE visit_id = %s", (visit_id,)).fetchone()
            raw = self._raw_of(conn, visit_id, "symptom")
        if row is None or row["symptom_received_at"] is None or raw is None:
            raise LookupError(f"no symptom check for visit {visit_id}")
        return StoredSymptomCheck(visit_id, row["symptom_received_at"], raw)

    def symptoms_waiting_for_processing(self, received_before: datetime) -> list[int]:
        """Visits whose symptom check arrived before the cutoff and has never been processed, oldest first."""
        with self._db.transaction() as conn:
            rows = conn.execute(
                "SELECT v.visit_id FROM visits v WHERE v.symptom_received_at < %s AND NOT EXISTS ("
                "SELECT 1 FROM record_processing p WHERE p.visit_id = v.visit_id AND p.analysis = 'symptom') "
                "ORDER BY v.symptom_received_at, v.visit_id", (received_before,)).fetchall()
        return [r["visit_id"] for r in rows]

    def save_rejected(self, source_id: int | None, raw: bytes, errors: list[dict[str, Any]],
                      blob_path: str | None) -> None:
        with self._db.transaction() as conn:
            conn.execute("INSERT INTO rejected_payloads (source_id, raw_bytes, errors, blob_path) "
                         "VALUES (%s, %s, %s, %s)", (source_id, raw, Jsonb(errors), blob_path))
