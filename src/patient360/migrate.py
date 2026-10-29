"""Apply the SQL files in ``migrations/`` once each: ``python -m patient360.migrate``.

Reads only the standard PG* variables (the job needs no other setting). What ran is recorded in ``schema_migrations``;
an advisory lock makes two instances starting together run each file once.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import psycopg
from psycopg import sql as pgsql
from psycopg.conninfo import make_conninfo

LOCK_ID = 360_001_001
SCHEMA_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")


def schema_from_env(env: dict[str, str] | os._Environ[str] = os.environ) -> str | None:
    """``PGSCHEMA``: the schema that holds our tables (empty = the default ``public``). A name is checked, never quoted
    blindly."""
    schema = (env.get("PGSCHEMA") or "").strip()
    if schema and not SCHEMA_NAME.fullmatch(schema):
        raise ValueError("PGSCHEMA must be a lower-case name of letters, digits and underscores")
    return schema or None


def use_schema(conn: psycopg.Connection, schema: str | None) -> None:
    """Make ``schema`` (created if absent) the one this connection reads and writes."""
    if schema:
        conn.execute(pgsql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(pgsql.Identifier(schema)))
        conn.execute(pgsql.SQL("SET search_path TO {}").format(pgsql.Identifier(schema)))
BASELINE = "001_init"
DEFAULT_DIRECTORY = Path(__file__).resolve().parents[2] / "migrations"
REQUIRED = ("PGHOST", "PGDATABASE", "PGUSER", "PGPASSWORD")


class MigrationError(Exception):
    """A migration file failed; ``version`` is its name."""

    def __init__(self, version: str, cause: Exception) -> None:
        super().__init__(f"{version}: {type(cause).__name__}: {cause}")
        self.version = version


def migrate(dsn: str, directory: Path, schema: str | None = None) -> list[str]:
    """Apply the files not yet recorded, in name order. Returns the versions applied now. ``schema``: where the tables
    (and ``schema_migrations``) live; made if absent."""
    applied: list[str] = []
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
        try:
            use_schema(conn, schema)
            conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, "
                         "applied_at timestamptz NOT NULL DEFAULT now())")
            done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
            # Applied by hand with psql before this runner existed: record it, never run it again
            if not done and conn.execute("SELECT to_regclass('visits')").fetchone()[0] is not None:
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (BASELINE,))
                done.add(BASELINE)
                print(f"baselined {BASELINE}")
            for path in sorted(directory.glob("*.sql")):
                version = path.stem
                if version in done:
                    print(f"skipped {version} (already applied)")
                    continue
                sql = path.read_text(encoding="utf-8-sig")
                if not re.search(r"^\s*BEGIN\b", sql, re.IGNORECASE | re.MULTILINE):
                    sql = f"BEGIN;\n{sql}\n;COMMIT;"
                try:
                    conn.execute(sql)
                    conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
                except psycopg.Error as error:
                    conn.execute("ROLLBACK")  # the script's own BEGIN leaves the session in a failed transaction
                    raise MigrationError(version, error) from error
                applied.append(version)
                print(f"applied {version}")
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
    if not applied:
        print("no migrations to apply")
    return applied


def main() -> int:
    missing = [name for name in REQUIRED if not os.environ.get(name)]
    if missing:
        print(f"missing environment variables: {', '.join(missing)}", file=sys.stderr)
        return 2
    dsn = make_conninfo(host=os.environ["PGHOST"], port=os.environ.get("PGPORT", "5432"),
                        dbname=os.environ["PGDATABASE"], user=os.environ["PGUSER"],
                        password=os.environ["PGPASSWORD"], sslmode=os.environ.get("PGSSLMODE", "prefer"))
    directory = Path(os.environ.get("MIGRATIONS_DIR") or DEFAULT_DIRECTORY)
    try:
        migrate(dsn, directory, schema_from_env())
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    except MigrationError as error:
        print(f"migration failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
