from datetime import timedelta

from patient360.adapters.postgres.db import (
    CLAIM_TIMEOUT,
    current_payload,
    insert_payload,
    record_attempt,
    set_stage,
    sha256_hex,
    source_priority_sql,
)
from tests.adapters.postgres import seed


def test_every_test_gets_its_own_database_with_the_schema_and_the_lookup_rows(db):
    assert [r["source_name"] for r in seed.fetch(db, "SELECT source_name FROM sources ORDER BY source_id")] == [
        "urgent", "batch", "symptom-checker", "on-demand"]
    seed.add_visit(db, 1)
    assert len(seed.fetch(db, "SELECT * FROM visits")) == 1


def test_a_second_database_does_not_see_the_rows_of_the_first(db):
    assert seed.fetch(db, "SELECT * FROM visits") == []


def test_payload_versions_count_up_and_history_is_kept(db):
    seed.add_visit(db, 1)
    with db.transaction() as conn:
        assert insert_payload(conn, 1, "visit", "processed", {"a": 1}) == 1
        assert insert_payload(conn, 1, "visit", "processed", {"a": 2}) == 2
        assert insert_payload(conn, 1, "symptom", "processed", {"s": 1}) == 1       # its own count per analysis
        assert current_payload(conn, 1, "visit", "processed") == {"a": 2}
        assert current_payload(conn, 1, "visit", "output_public") is None
    assert len(seed.fetch(db, "SELECT * FROM record_payloads WHERE kind = 'processed'")) == 3


def test_the_raw_payload_keeps_its_bytes_and_the_fingerprint_is_of_those_bytes(db):
    seed.add_visit(db, 1)
    raw = seed.add_raw(db, 1, {"visit_id": 1})
    [row] = seed.fetch(db, "SELECT raw_bytes, sha256 FROM record_payloads WHERE kind = 'raw'")
    assert bytes(row["raw_bytes"]) == raw and row["sha256"] == sha256_hex(raw)


def test_stage_and_attempt_helpers_write_one_row_each(db):
    seed.add_visit(db, 1)
    with db.transaction() as conn:
        set_stage(conn, 1, "processed")
        record_attempt(conn, 1, "visit", 1, "retry", "S3Unavailable")
    assert seed.fetch(db, "SELECT stage FROM visits")[0]["stage"] == "processed"
    [attempt] = seed.fetch(db, "SELECT attempt, outcome, error FROM record_processing_attempts")
    assert (attempt["attempt"], attempt["outcome"], attempt["error"]) == (1, "retry", "S3Unavailable")


def test_a_transaction_that_fails_leaves_nothing_behind(db):
    seed.add_visit(db, 1)
    try:
        with db.transaction() as conn:
            set_stage(conn, 1, "processed")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert seed.fetch(db, "SELECT stage FROM visits")[0]["stage"] == "received"


def test_the_priority_sql_ranks_urgent_first_and_batch_last(db):
    sql = f"SELECT source_name FROM sources ORDER BY {source_priority_sql('source_name')}"
    assert [r["source_name"] for r in seed.fetch(db, sql)] == ["urgent", "symptom-checker", "on-demand", "batch"]


def test_the_claim_timeout_is_ten_minutes():
    assert CLAIM_TIMEOUT == timedelta(minutes=10)
