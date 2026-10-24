"""Plain-SQL helpers that put rows in a test database, so a test of one part of the store does not need the others."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from patient360.adapters.postgres.db import Database, insert_payload, sha256_hex

LONG_AGO = datetime(2026, 1, 1, tzinfo=UTC)


def add_category(db: Database, category_id: int, name: str, active: bool = True) -> None:
    with db.transaction() as conn:
        conn.execute("INSERT INTO categories (category_id, category_name, active) VALUES (%s, %s, %s)",
                     (category_id, name, active))


def add_test(db: Database, test_id: int, name_en: str, *, name_ar: str | None = None, description: str | None = None,
             categories: tuple[int, ...] = (), active: bool = True) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO tests (test_id, test_name_en, test_name_ar, test_description_en, category_id, active) "
            "VALUES (%s, %s, %s, %s, %s, %s)", (test_id, name_en, name_ar, description, list(categories), active))


def add_parameter(db: Database, parameter_id: int, name: str, *, categories: tuple[int, ...] = (),
                  active: bool = True) -> None:
    with db.transaction() as conn:
        conn.execute("INSERT INTO parameters (parameter_id, parameter_name, category_id, active) "
                     "VALUES (%s, %s, %s, %s)", (parameter_id, name, list(categories), active))


def add_visit(db: Database, visit_id: int, *, patient_id: int | None = None, source_id: int = 1,
              key: str | None = None, stage: str = "received", tier: str | None = None,
              created_at: datetime = LONG_AGO, company_id: int | None = None, batch_id: str | None = None) -> str:
    """A visit row. Returns its idempotency key (default: ``key-<visit_id>``)."""
    key = key or f"key-{visit_id}"
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO visits (visit_id, patient_id, idempotency_key, source_id, company_id, tier, batch_id, stage, "
            "created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (visit_id, patient_id or visit_id + 1_000_000, key, source_id, company_id, tier, batch_id, stage,
             created_at))
    return key


def add_raw(db: Database, visit_id: int, payload: dict[str, Any]) -> bytes:
    """The raw payload of a visit, as the Backend would have sent it. Returns the bytes."""
    raw = json.dumps(payload).encode()
    with db.transaction() as conn:
        insert_payload(conn, visit_id, "visit", "raw", payload, raw_bytes=raw)
    return raw


def add_payload(db: Database, visit_id: int, kind: str, body: dict[str, Any], analysis: str = "visit") -> int:
    with db.transaction() as conn:
        return insert_payload(conn, visit_id, analysis, kind, body)


def add_processing(db: Database, visit_id: int, *, analysis: str = "visit", step: str = "upload",
                   status: str = "pending", read_from: str = "postgresql", attempts: int = 0,
                   next_retry_at: datetime | None = None, updated_at: datetime | None = None) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO record_processing (visit_id, analysis, step, read_from, status, attempts, next_retry_at, "
            "updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, coalesce(%s, now()))",
            (visit_id, analysis, step, read_from, status, attempts, next_retry_at, updated_at))


def add_batch(db: Database, batch_id: str, *, status: str = "created", read_from: str = "postgresql",
              created_at: datetime = LONG_AGO) -> None:
    with db.transaction() as conn:
        conn.execute("INSERT INTO batches (batch_id, status, read_from, created_at) VALUES (%s, %s, %s, %s)",
                     (batch_id, status, read_from, created_at))


def fetch(db: Database, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    """Read rows back, to check what the store wrote."""
    with db.transaction() as conn:
        return conn.execute(sql, params).fetchall()


__all__ = ["Jsonb", "sha256_hex", "add_batch", "add_category", "add_parameter", "add_payload", "add_processing",
           "add_raw", "add_test", "add_visit", "fetch"]
