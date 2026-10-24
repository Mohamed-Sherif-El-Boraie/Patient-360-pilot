"""PostgreSQL access: a connection pool and the few helpers every part of the store shares.

Every store method opens ONE transaction with ``db.transaction()``: it commits when the block ends and rolls back if an
exception leaves it, so a method either does all its writes or none. Rows come back as dicts.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
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
    ``raw_bytes`` is for kind ``raw`` only: exactly what the Backend sent. The fingerprint is of those bytes, or of the
    JSON text otherwise."""
    version = conn.execute(
        "SELECT coalesce(max(version), 0) + 1 AS version FROM record_payloads "
        "WHERE visit_id = %s AND analysis = %s AND kind = %s", (visit_id, analysis, kind)).fetchone()["version"]
    fingerprint = sha256_hex(raw_bytes if raw_bytes is not None else
                             json.dumps(body, sort_keys=True, ensure_ascii=False).encode())
    conn.execute(
        "INSERT INTO record_payloads (visit_id, analysis, kind, version, body, raw_bytes, sha256) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (visit_id, analysis, kind, version, Jsonb(body), raw_bytes, fingerprint))
    return version


def current_payload(conn: psycopg.Connection[dict[str, Any]], visit_id: int, analysis: Analysis,
                    kind: PayloadKind) -> dict[str, Any] | None:
    """The newest version of a JSON document of the visit, or ``None``."""
    row = conn.execute("SELECT body FROM record_payloads_current WHERE visit_id = %s AND analysis = %s AND kind = %s",
                       (visit_id, analysis, kind)).fetchone()
    return row["body"] if row else None


def set_stage(conn: psycopg.Connection[dict[str, Any]], visit_id: int, stage: str) -> None:
    """Move the visit's progress marker (for people reading the table; the work queues use ``record_processing``)."""
    conn.execute("UPDATE visits SET stage = %s, updated_at = now() WHERE visit_id = %s", (stage, visit_id))


def record_attempt(conn: psycopg.Connection[dict[str, Any]], visit_id: int, analysis: Analysis, attempt: int,
                   outcome: Literal["sent", "retry", "failed"], error: str | None = None) -> None:
    """One line in the insert-only history of tries."""
    conn.execute(
        "INSERT INTO record_processing_attempts (visit_id, analysis, attempt, finished_at, outcome, error) "
        "VALUES (%s, %s, %s, now(), %s, %s)", (visit_id, analysis, attempt, outcome, error))
