"""A real PostgreSQL for the adapter tests: a fresh database per test, made from a template that has the migration.

The server is the Docker container ``pg-test`` (port 5433). Another server can be used with ``TEST_PG_ADMIN_DSN``. If no
server answers, the tests are skipped. Each pytest session makes its own template, so two sessions can run at once.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from patient360.adapters.postgres.db import Database

# 127.0.0.1, not "localhost": on Windows "localhost" tries IPv6 first and every connection waits 3 seconds
ADMIN_DSN = os.environ.get("TEST_PG_ADMIN_DSN", "postgresql://postgres:test@127.0.0.1:5433/postgres")
MIGRATION = Path(__file__).parents[3] / "migrations" / "001_init.sql"


def _admin():
    return psycopg.connect(ADMIN_DSN, autocommit=True, connect_timeout=3)


@pytest.fixture(scope="session")
def pg_template():
    name = f"p360_tpl_{uuid.uuid4().hex[:10]}"
    try:
        admin = _admin()
    except psycopg.OperationalError as error:
        pytest.skip(f"no PostgreSQL to test against ({ADMIN_DSN}): {error}")
    with admin:
        admin.execute(f'CREATE DATABASE "{name}"')
        with psycopg.connect(make_conninfo(ADMIN_DSN, dbname=name), autocommit=True) as conn:
            conn.execute(MIGRATION.read_text(encoding="utf-8"))
        yield name
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def db(pg_template):
    """A new empty database (the migration applied) for one test, as a ``Database``."""
    name = f"p360_t_{uuid.uuid4().hex[:10]}"
    with _admin() as admin:
        admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{pg_template}"')
        database = Database.connect(make_conninfo(ADMIN_DSN, dbname=name), min_size=1, max_size=4)
        try:
            yield database
        finally:
            database.close()
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
