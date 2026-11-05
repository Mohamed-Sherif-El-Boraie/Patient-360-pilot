"""PostgreSQL access: a connection pool and the few helpers every part of the store shares.

Every store method opens ONE transaction with ``db.transaction()``: it commits when the block ends and rolls back if an
exception leaves it, so a method either does all its writes or none. Rows come back as dicts.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Literal

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from patient360.domain.rules.dispatch import SOURCE_PRIORITY

CLAIM_TIMEOUT = timedelta(minutes=10)
"""A unit that has been ``processing`` longer than this was left by a worker that died: it is free to claim again."""

Analysis = Literal["visit", "symptom"]
PayloadKind = Literal["raw", "processed", "output_internal", "output_public"]


class Database:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    @classmethod
    def connect(cls, dsn: str, *, min_size: int = 1, max_size: int = 10) -> Database:
        pool = ConnectionPool(dsn, min_size=min_size, max_size=max_size, kwargs={"row_factory": dict_row}, open=True)
        pool.wait(timeout=10)
        return cls(pool)

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection[dict[str, Any]]]:
        """One transaction on a pooled connection: commit at the end, rollback on an error."""
        with self._pool.connection() as conn:
            yield conn

    def close(self) -> None:
        self._pool.close()


def source_priority_sql(source_name_column: str) -> str:
    """A SQL ``CASE`` that turns a source name into its dispatch rank (urgent 0, symptom-checker 1, on-demand 2,
    batch 3). Built from the domain table, so SQL and Python can never disagree. The names are constants."""
    whens = " ".join(f"WHEN '{name}' THEN {rank}" for name, rank in SOURCE_PRIORITY.items())
    return f"CASE {source_name_column} {whens} END"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def insert_payload(conn: psycopg.Connection[dict[str, Any]], visit_id: int, analysis: Analysis, kind: PayloadKind,
                   body: dict[str, Any], raw_bytes: bytes | None = None) -> int:
    """Add a new version of a JSON document of the visit (history is kept) and return its version number.
    Two copies are kept: ``body`` (jsonb, searchable, not byte-exact) and ``raw_bytes`` (the JSON text as it is: the
    bytes received when given, else our own serialisation of ``body``). The fingerprint is of ``raw_bytes``."""
    version = conn.execute(
        "SELECT coalesce(max(version), 0) + 1 AS version FROM record_payloads "
        "WHERE visit_id = %s AND analysis = %s AND kind = %s", (visit_id, analysis, kind)).fetchone()["version"]
    if raw_bytes is None:
        raw_bytes = json.dumps(body, ensure_ascii=False).encode()
    fingerprint = sha256_hex(raw_bytes)
    conn.execute(
        "INSERT INTO record_payloads (visit_id, analysis, kind, version, body, raw_bytes, sha256) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (visit_id, analysis, kind, version, Jsonb(body), raw_bytes, fingerprint))
    return version


def current_payload(conn: psycopg.Connection[dict[str, Any]], visit_id: int, analysis: Analysis,
                    kind: PayloadKind) -> dict[str, Any] | None:
    """The newest version of a JSON document of the visit, or ``None``. Read from the text copy, so the keys keep the
    order they were written in (jsonb sorts them by length, then alphabetically)."""
    row = conn.execute("SELECT body, raw_bytes FROM record_payloads_current "
                       "WHERE visit_id = %s AND analysis = %s AND kind = %s", (visit_id, analysis, kind)).fetchone()
    if row is None:
        return None
    try:
        return json.loads(bytes(row["raw_bytes"]))
    except ValueError:          # a text that is not valid JSON (an AWS record we had to repair): the structured copy
        return row["body"]


def set_stage(conn: psycopg.Connection[dict[str, Any]], visit_id: int, stage: str) -> None:
    """Move the visit's progress marker (for people reading the table; the work queues use ``record_processing``)."""
    conn.execute("UPDATE visits SET stage = %s, updated_at = now() WHERE visit_id = %s", (stage, visit_id))


def record_attempt(conn: psycopg.Connection[dict[str, Any]], visit_id: int, analysis: Analysis, attempt: int,
                   outcome: Literal["sent", "retry", "failed"], error: str | None = None,
                   started_at: datetime | None = None) -> None:
    """One line in the insert-only history of tries. ``started_at`` is when the unit was claimed (default: now)."""
    conn.execute(
        "INSERT INTO record_processing_attempts (visit_id, analysis, attempt, started_at, finished_at, outcome, error) "
        "VALUES (%s, %s, %s, coalesce(%s, now()), now(), %s, %s)",
        (visit_id, analysis, attempt, started_at, outcome, error))


def record_phase(conn: psycopg.Connection[dict[str, Any]], visit_id: int, analysis: Analysis, phase: str,
                 attempt: int, started_at: datetime | None) -> None:
    """One row of ``record_phase_timings``: the phase began at ``started_at`` and ends now. Run again, it is replaced.
    No start time (a stage that never happened) means no row."""
    if started_at is None:
        return
    conn.execute(
        "INSERT INTO record_phase_timings (visit_id, analysis, phase, attempt, started_at, duration_ms) "
        "VALUES (%s, %s, %s, %s, %s, greatest(0, round(extract(epoch FROM now() - %s) * 1000))::int) "
        "ON CONFLICT (visit_id, analysis, phase, attempt) DO UPDATE "
        "SET started_at = excluded.started_at, duration_ms = excluded.duration_ms",
        (visit_id, analysis, phase, attempt, started_at, started_at))


def claimed_at(conn: psycopg.Connection[dict[str, Any]], visit_id: int, analysis: Analysis) -> datetime | None:
    """When the current unit of work was claimed (a claim sets ``updated_at``): read it BEFORE the result is saved."""
    row = conn.execute("SELECT updated_at FROM record_processing WHERE visit_id = %s AND analysis = %s",
                       (visit_id, analysis)).fetchone()
    return row["updated_at"] if row else None
