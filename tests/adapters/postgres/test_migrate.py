"""The migration runner against a real PostgreSQL: empty database, hand-applied database, failures, concurrency."""
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from patient360 import migrate as runner
from patient360.migrate import MigrationError, main, migrate
from tests.adapters.postgres.conftest import ADMIN_DSN, MIGRATION

INIT = MIGRATION.read_text(encoding="utf-8")


@pytest.fixture
def empty_dsn(pg_template):
    """DSN of a new database with nothing in it."""
    name = f"p360_m_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
        try:
            yield make_conninfo(ADMIN_DSN, dbname=name)
        finally:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def folder(tmp_path):
    (tmp_path / "001_init.sql").write_text(INIT, encoding="utf-8")
    return tmp_path


def _query(dsn, sql):
    with psycopg.connect(dsn) as conn:
        return [row[0] for row in conn.execute(sql)]


def _recorded(dsn):
    return _query(dsn, "SELECT version FROM schema_migrations ORDER BY version")


def _tables(dsn):
    return _query(dsn, "SELECT table_name FROM information_schema.tables "
                       "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")


def test_empty_database_gets_the_schema_and_a_second_run_does_nothing(empty_dsn):
    assert migrate(empty_dsn, MIGRATION.parent) == ["001_init"]
    assert len(_tables(empty_dsn)) == 29 + 1  # the schema's 29 tables and schema_migrations
    assert _recorded(empty_dsn) == ["001_init"]
    assert migrate(empty_dsn, MIGRATION.parent) == []


def test_hand_applied_database_is_baselined_without_running(db, capsys):
    dsn = db._pool.conninfo
    assert migrate(dsn, MIGRATION.parent) == []
    assert "baselined 001_init" in capsys.readouterr().out
    assert _recorded(dsn) == ["001_init"]
    assert migrate(dsn, MIGRATION.parent) == []
    assert "skipped 001_init (already applied)" in capsys.readouterr().out


def test_later_files_run_after_the_first_with_or_without_their_own_transaction(empty_dsn, folder):
    (folder / "002_test.sql").write_text("CREATE TABLE t2 (id int);", encoding="utf-8")
    (folder / "003_test.sql").write_text("BEGIN;\nCREATE TABLE t3 (id int);\nCOMMIT;", encoding="utf-8")
    assert migrate(empty_dsn, folder) == ["001_init", "002_test", "003_test"]
    assert {"t2", "t3"} <= set(_tables(empty_dsn))
    assert _recorded(empty_dsn) == ["001_init", "002_test", "003_test"]


def test_a_broken_file_stops_the_run(empty_dsn, folder):
    (folder / "002_bad.sql").write_text("CREATE TABLE t2 (id int); SELECT * FROM no_such_table;", encoding="utf-8")
    (folder / "003_after.sql").write_text("CREATE TABLE t3 (id int);", encoding="utf-8")
    with pytest.raises(MigrationError, match="002_bad: UndefinedTable"):
        migrate(empty_dsn, folder)
    assert _recorded(empty_dsn) == ["001_init"]
    assert not {"t2", "t3"} & set(_tables(empty_dsn))  # the failed file was rolled back, the next never ran


def test_a_leading_byte_order_mark_is_ignored(empty_dsn, folder):
    (folder / "001_init.sql").write_bytes(b"\xef\xbb\xbf" + INIT.encode("utf-8"))
    assert migrate(empty_dsn, folder) == ["001_init"]


def test_two_runs_at_once_apply_the_first_file_once(empty_dsn, folder):
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: migrate(empty_dsn, folder), range(2)))
    assert sorted(results) == [[], ["001_init"]]
    assert _recorded(empty_dsn) == ["001_init"]


def test_missing_variables_exit_with_2_and_print_no_value(monkeypatch, capsys):
    for name in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD", "PGSSLMODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PGPASSWORD", "s3cret-value")
    assert main() == 2
    err = capsys.readouterr().err
    assert "PGHOST" in err and "PGDATABASE" in err and "PGUSER" in err
    assert "s3cret-value" not in err


def test_main_applies_with_standard_variables(empty_dsn, monkeypatch, capsys):
    info = psycopg.conninfo.conninfo_to_dict(empty_dsn)
    for name, key in (("PGHOST", "host"), ("PGPORT", "port"), ("PGDATABASE", "dbname"), ("PGUSER", "user"),
                      ("PGPASSWORD", "password")):
        monkeypatch.setenv(name, str(info[key]))
    monkeypatch.setenv("MIGRATIONS_DIR", str(Path(MIGRATION).parent))
    assert main() == 0
    assert "applied 001_init" in capsys.readouterr().out
    assert runner.DEFAULT_DIRECTORY == MIGRATION.parent
