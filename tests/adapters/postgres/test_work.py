"""The upload work queue against a real PostgreSQL: processed requests, batches, claims and the result of an upload."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from patient360.adapters.postgres.work import WorkMixin
from patient360.ports import BatchVisit, WorkUnit
from tests.adapters.postgres.seed import add_batch, add_payload, add_processing, add_visit, fetch

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
T0 = datetime(2026, 10, 1, tzinfo=UTC)
URGENT, BATCH, SYMPTOM, ON_DEMAND = 1, 2, 3, 4


class Store(WorkMixin):
    def __init__(self, db) -> None:
        self._db = db


@pytest.fixture
def store(db):
    return Store(db)


def add_processed(db, visit_id, *, source_id=URGENT, tier="NORMAL", age=0, **processing):
    """A visit that finished processing: stage processed, one processed request, a pending upload row.
    ``age`` is how many minutes after T0 it was received (older = smaller)."""
    key = add_visit(db, visit_id, source_id=source_id, tier=tier, stage="processed",
                    created_at=T0 + timedelta(minutes=age))
    add_payload(db, visit_id, "processed", {"visit": visit_id})
    add_processing(db, visit_id, **processing)
    return key


def add_symptom_unit(db, visit_id, *, received_age=0, **processing):
    """A symptom check ready to upload, for an existing visit."""
    with db.transaction() as conn:
        conn.execute("UPDATE visits SET symptom_received_at = %s WHERE visit_id = %s",
                     (T0 + timedelta(minutes=received_age), visit_id))
    add_payload(db, visit_id, "processed", {"symptom": visit_id}, analysis="symptom")
    add_processing(db, visit_id, analysis="symptom", **processing)


def processing_row(db, visit_id, analysis="visit"):
    return fetch(db, "SELECT * FROM record_processing WHERE visit_id = %s AND analysis = %s", (visit_id, analysis))[0]


def stage_of(db, visit_id):
    return fetch(db, "SELECT stage FROM visits WHERE visit_id = %s", (visit_id,))[0]["stage"]


def attempts_of(db, visit_id, analysis="visit"):
    return [(r["attempt"], r["outcome"], r["error"]) for r in fetch(
        db, "SELECT attempt, outcome, error FROM record_processing_attempts WHERE visit_id = %s AND analysis = %s "
            "ORDER BY attempt", (visit_id, analysis))]


# ---- save_processed / save_symptom_processed ------------------------------------------------------
def test_save_processed_sets_tier_payload_and_pending_row(db, store):
    add_visit(db, 1, source_id=BATCH)
    store.save_processed(1, "CRITICAL", {"a": 1}, "blob")
    visit = fetch(db, "SELECT tier, lane, stage FROM visits WHERE visit_id = 1")[0]
    assert (visit["tier"], visit["lane"], visit["stage"]) == ("CRITICAL", "critical", "processed")
    payload = fetch(db, "SELECT body FROM record_payloads WHERE visit_id = 1 AND kind = 'processed'")[0]
    assert payload["body"] == {"a": 1}
    row = processing_row(db, 1)
    assert (row["step"], row["status"], row["read_from"], row["attempts"]) == ("upload", "pending", "blob", 0)


def test_save_processed_twice_adds_a_version_and_resets_the_row(db, store):
    add_visit(db, 1)
    store.save_processed(1, "NORMAL", {"v": 1}, "blob")
    with db.transaction() as conn:
        conn.execute("UPDATE record_processing SET status = 'failed', attempts = 3, last_error = 'x', "
                     "claimed_by = 'urgent', next_retry_at = now() WHERE visit_id = 1")
    store.save_processed(1, "MILD", {"v": 2}, "postgresql")
    versions = fetch(db, "SELECT version, body FROM record_payloads WHERE visit_id = 1 AND kind = 'processed' "
                         "ORDER BY version")
    assert [(r["version"], r["body"]) for r in versions] == [(1, {"v": 1}), (2, {"v": 2})]
    row = processing_row(db, 1)
    assert (row["status"], row["attempts"], row["last_error"], row["claimed_by"], row["next_retry_at"],
            row["read_from"]) == ("pending", 0, None, None, None, "postgresql")
    assert fetch(db, "SELECT tier FROM visits WHERE visit_id = 1")[0]["tier"] == "MILD"


def test_save_processed_failing_write_leaves_nothing(db, store):
    add_visit(db, 1)
    with pytest.raises(psycopg.errors.CheckViolation):
        store.save_processed(1, "CRITICAL", {"a": 1}, "nowhere")
    assert fetch(db, "SELECT tier FROM visits WHERE visit_id = 1")[0]["tier"] is None
    assert fetch(db, "SELECT 1 FROM record_payloads WHERE visit_id = 1") == []
    assert stage_of(db, 1) == "received"


def test_save_symptom_processed(db, store):
    add_visit(db, 1, tier="MILD")
    store.save_symptom_processed(1, {"s": 1}, "blob")
    store.save_symptom_processed(1, {"s": 2}, "postgresql")
    bodies = fetch(db, "SELECT body FROM record_payloads WHERE visit_id = 1 AND analysis = 'symptom' "
                       "AND kind = 'processed' ORDER BY version")
    assert [r["body"] for r in bodies] == [{"s": 1}, {"s": 2}]
    row = processing_row(db, 1, "symptom")
    assert (row["status"], row["read_from"], row["attempts"]) == ("pending", "postgresql", 0)
    assert stage_of(db, 1) == "processed_symptom"
    assert fetch(db, "SELECT tier FROM visits WHERE visit_id = 1")[0]["tier"] == "MILD"


# ---- claim_batch ----------------------------------------------------------------------------------
def test_claim_batch_returns_none_below_size_and_writes_nothing(db, store):
    add_processed(db, 1, source_id=BATCH)
    assert store.claim_batch("normal", 2) is None
    assert fetch(db, "SELECT 1 FROM batches") == []
    assert fetch(db, "SELECT batch_id FROM visits WHERE visit_id = 1")[0]["batch_id"] is None
    assert stage_of(db, 1) == "processed"


def test_claim_batch_takes_the_oldest_in_order_and_marks_them(db, store):
    keys = {i: add_processed(db, i, source_id=BATCH, age=age) for i, age in {1: 5, 2: 1, 3: 3, 4: 1}.items()}
    claimed = store.claim_batch("normal", 3)
    assert claimed.batch_id == "normal_000001"
    assert claimed.visits == [BatchVisit(keys[2], {"visit": 2}), BatchVisit(keys[4], {"visit": 4}),
                              BatchVisit(keys[3], {"visit": 3})]
    batch = fetch(db, "SELECT created_at, status FROM batches")[0]
    assert (claimed.created_at, batch["status"]) == (batch["created_at"], "created")
    visits = fetch(db, "SELECT visit_id, batch_id, stage FROM visits")
    assert {r["visit_id"]: (r["batch_id"], r["stage"]) for r in visits} == {
        1: (None, "processed"), 2: ("normal_000001", "queued"), 3: ("normal_000001", "queued"),
        4: ("normal_000001", "queued")}


def test_claim_batch_numbers_each_lane_separately(db, store):
    add_processed(db, 1, source_id=BATCH, tier="NORMAL")
    add_processed(db, 2, source_id=BATCH, tier="CRITICAL")
    add_processed(db, 3, source_id=BATCH, tier="MODERATE")
    assert store.claim_batch("normal", 1).batch_id == "normal_000001"
    assert store.claim_batch("critical", 1).batch_id == "critical_000001"
    assert store.claim_batch("critical", 1).batch_id == "critical_000002"


def test_claim_batch_ignores_visits_that_do_not_qualify(db, store):
    add_processed(db, 1, source_id=BATCH, status="processing")                    # upload already under way
    add_processed(db, 2, source_id=BATCH, tier="CRITICAL")                        # other lane
    add_processed(db, 3, source_id=URGENT)                                        # not a batch visit
    add_visit(db, 4, source_id=BATCH, tier="NORMAL", stage="validated")           # not processed: no work row
    add_batch(db, "normal_000009")
    add_processed(db, 5, source_id=BATCH)
    with db.transaction() as conn:
        conn.execute("UPDATE visits SET batch_id = 'normal_000009' WHERE visit_id = 5")   # already batched
    add_processed(db, 6, source_id=BATCH)
    assert store.claim_batch("normal", 2) is None
    assert [b.model_input for b in store.claim_batch("normal", 1).visits] == [{"visit": 6}]


def test_claim_batch_skips_rows_locked_by_another_session(db, store):
    for i in range(1, 5):
        add_processed(db, i, source_id=BATCH, age=i)
    with db.transaction() as other:
        other.execute("SELECT 1 FROM visits WHERE visit_id IN (1, 2) FOR UPDATE").fetchall()
        first = store.claim_batch("normal", 2)
        assert [b.model_input for b in first.visits] == [{"visit": 3}, {"visit": 4}]
        assert store.claim_batch("normal", 2) is None            # only the two locked rows are left
    second = store.claim_batch("normal", 2)
    assert [b.model_input for b in second.visits] == [{"visit": 1}, {"visit": 2}]


def test_claim_batch_failure_rolls_back(db, store):
    add_processed(db, 1, source_id=BATCH)
    with db.transaction() as conn:
        conn.execute("INSERT INTO batches (batch_id) VALUES ('normal_000001')")        # the next id is taken
    with pytest.raises(psycopg.errors.UniqueViolation):
        store.claim_batch("normal", 1)
    assert fetch(db, "SELECT batch_id FROM visits WHERE visit_id = 1")[0]["batch_id"] is None
    assert stage_of(db, 1) == "processed"


def test_set_batch_read_from(db, store):
    add_batch(db, "normal_000001")
    store.set_batch_read_from("normal_000001", "blob")
    assert fetch(db, "SELECT read_from FROM batches")[0]["read_from"] == "blob"


def test_batch_model_inputs_in_batch_order(db, store):
    keys = {i: add_processed(db, i, source_id=BATCH, age=age) for i, age in {1: 3, 2: 1, 3: 1}.items()}
    claimed = store.claim_batch("normal", 3)
    add_payload(db, 3, "processed", {"visit": 3, "newer": True})                          # the current one wins
    assert store.batch_model_inputs(claimed.batch_id) == [
        BatchVisit(keys[2], {"visit": 2}), BatchVisit(keys[3], {"visit": 3, "newer": True}),
        BatchVisit(keys[1], {"visit": 1})]


# ---- claim_next_work ------------------------------------------------------------------------------
def test_claim_next_work_returns_none_when_idle(store):
    assert store.claim_next_work(NOW) is None


def test_claim_next_work_builds_a_visit_unit_and_marks_it(db, store):
    key = add_processed(db, 1, source_id=ON_DEMAND, tier="CRITICAL", age=7, read_from="blob")
    unit = store.claim_next_work(NOW)
    assert unit == WorkUnit("on-demand", "critical", T0 + timedelta(minutes=7), "blob", 0, visit_id=1,
                            idempotency_key=key)
    row = processing_row(db, 1)
    assert (row["status"], row["claimed_by"], row["updated_at"]) == ("processing", "on-demand", NOW)
    assert stage_of(db, 1) == "queued"
    assert store.model_input_of(unit) == {"visit": 1}


def test_claim_next_work_priority_urgent_symptom_on_demand_then_batch(db, store):
    add_processed(db, 1, source_id=ON_DEMAND, age=1)
    add_processed(db, 2, source_id=URGENT, age=9)
    add_processed(db, 3, source_id=URGENT, age=2)         # symptom unit of an old on-demand visit below
    add_symptom_unit(db, 1, received_age=8)
    add_processed(db, 4, source_id=BATCH, age=0)
    add_batch(db, "normal_000001", created_at=T0 - timedelta(days=1))                 # older than everything
    order = [store.claim_next_work(NOW) for _ in range(5)]
    assert [(u.source, u.visit_id, u.batch_id, u.analysis) for u in order] == [
        ("urgent", 3, None, "visit"), ("urgent", 2, None, "visit"), ("symptom-checker", 1, None, "symptom"),
        ("on-demand", 1, None, "visit"), ("batch", None, "normal_000001", "visit")]
    assert store.claim_next_work(NOW) is None                  # visit 4 travels in its batch, never alone


def test_claim_next_work_same_age_goes_by_visit_id(db, store):
    add_processed(db, 2, age=1)
    add_processed(db, 1, age=1)
    assert [store.claim_next_work(NOW).visit_id for _ in range(2)] == [1, 2]


def test_claim_next_work_symptom_unit_fields(db, store):
    key = add_processed(db, 1, source_id=URGENT, tier="MODERATE", age=1)
    add_symptom_unit(db, 1, received_age=30, read_from="blob", attempts=2)
    assert store.claim_next_work(NOW).analysis == "visit"                             # urgent visit first
    unit = store.claim_next_work(NOW)
    assert unit == WorkUnit("symptom-checker", "critical", T0 + timedelta(minutes=30), "blob", 2, visit_id=1,
                            idempotency_key=key, analysis="symptom")
    assert processing_row(db, 1, "symptom")["claimed_by"] == "symptom-checker"
    assert stage_of(db, 1) == "queued_symptom"
    assert store.model_input_of(unit) == {"symptom": 1}


def test_claim_next_work_batch_unit_fields(db, store):
    add_batch(db, "critical_000004", read_from="blob", created_at=T0)
    unit = store.claim_next_work(NOW)
    assert unit == WorkUnit("batch", "critical", T0, "blob", 0, batch_id="critical_000004")
    assert fetch(db, "SELECT claimed_at FROM batches")[0]["claimed_at"] == NOW
    assert store.claim_next_work(NOW) is None


def test_claim_next_work_ignores_other_steps_and_states(db, store):
    add_processed(db, 1, step="deliver")
    add_processed(db, 2, status="sent")
    add_processed(db, 3, status="failed")
    add_batch(db, "normal_000001", status="submitted")
    assert store.claim_next_work(NOW) is None


def test_claim_next_work_respects_retry_time(db, store):
    add_processed(db, 1, next_retry_at=NOW + timedelta(minutes=1))
    add_batch(db, "normal_000001")
    with db.transaction() as conn:
        conn.execute("UPDATE batches SET next_retry_at = %s", (NOW + timedelta(minutes=1),))
    assert store.claim_next_work(NOW) is None
    assert store.claim_next_work(NOW + timedelta(minutes=1)).visit_id == 1
    assert store.claim_next_work(NOW + timedelta(minutes=1)).batch_id == "normal_000001"


def test_claim_next_work_reclaims_only_stale_processing_units(db, store):
    add_processed(db, 1, status="processing", updated_at=NOW - timedelta(minutes=11))
    add_processed(db, 2, status="processing", updated_at=NOW - timedelta(minutes=1))
    add_batch(db, "normal_000001")
    add_batch(db, "normal_000002")
    with db.transaction() as conn:
        for batch_id, minutes in (("normal_000001", 11), ("normal_000002", 1)):
            conn.execute("UPDATE batches SET claimed_at = %s WHERE batch_id = %s",
                         (NOW - timedelta(minutes=minutes), batch_id))
    claimed = [store.claim_next_work(NOW) for _ in range(3)]
    assert [(u.visit_id, u.batch_id) for u in claimed[:2]] == [(1, None), (None, "normal_000001")]
    assert claimed[2] is None


def test_claim_next_work_never_returns_the_same_unit_twice(db, store):
    for i in range(1, 4):
        add_processed(db, i, age=i)
    seen = [store.claim_next_work(NOW).visit_id for _ in range(3)]
    assert seen == [1, 2, 3]
    assert store.claim_next_work(NOW) is None


def test_claim_next_work_skips_units_locked_by_another_session(db, store):
    add_processed(db, 1, age=1)
    add_processed(db, 2, age=2)
    add_batch(db, "normal_000001")
    with db.transaction() as other:
        other.execute("SELECT 1 FROM record_processing WHERE visit_id = 1 FOR UPDATE").fetchall()
        assert store.claim_next_work(NOW).visit_id == 2
        assert store.claim_next_work(NOW).batch_id == "normal_000001"      # visit 1 is locked, so the batch is next
        assert store.claim_next_work(NOW) is None
    assert store.claim_next_work(NOW).visit_id == 1


def test_model_input_of_unknown_unit_raises(store):
    with pytest.raises(LookupError):
        store.model_input_of(WorkUnit("urgent", "normal", NOW, "blob", 0, visit_id=99, idempotency_key="k"))


# ---- mark_sent / mark_failed_attempt --------------------------------------------------------------
def claimed_unit(db, store, visit_id=1):
    add_processed(db, visit_id)
    return store.claim_next_work(NOW)


def test_mark_sent_for_a_visit(db, store):
    unit = claimed_unit(db, store)
    store.mark_sent(unit)
    assert processing_row(db, 1)["status"] == "sent"
    assert stage_of(db, 1) == "sent_to_aws"
    assert attempts_of(db, 1) == [(1, "sent", None)]


def test_mark_sent_for_a_symptom_unit_leaves_the_visit_row_alone(db, store):
    add_processed(db, 1)
    add_symptom_unit(db, 1, attempts=1)
    unit = [store.claim_next_work(NOW) for _ in range(2)][1]
    assert unit.analysis == "symptom"
    store.mark_sent(unit)
    assert processing_row(db, 1, "symptom")["status"] == "sent"
    assert processing_row(db, 1)["status"] == "processing"
    assert stage_of(db, 1) == "sent_to_aws_symptom"
    assert attempts_of(db, 1, "symptom") == [(2, "sent", None)]
    assert attempts_of(db, 1) == []


def test_mark_failed_attempt_retry_then_give_up_for_a_visit(db, store):
    unit = claimed_unit(db, store)
    retry_at = NOW + timedelta(minutes=5)
    store.mark_failed_attempt(unit, "S3Unavailable", retry_at)
    row = processing_row(db, 1)
    assert (row["status"], row["attempts"], row["last_error"], row["claimed_by"], row["next_retry_at"]) == (
        "pending", 1, "S3Unavailable", None, retry_at)
    assert stage_of(db, 1) == "queued"                                   # only giving up changes the stage
    retried = store.claim_next_work(retry_at)
    assert retried.attempts == 1
    store.mark_failed_attempt(retried, "S3Unavailable", None)
    row = processing_row(db, 1)
    assert (row["status"], row["attempts"], row["next_retry_at"]) == ("failed", 2, None)
    assert stage_of(db, 1) == "failed"
    assert attempts_of(db, 1) == [(1, "retry", "S3Unavailable"), (2, "failed", "S3Unavailable")]
    assert store.claim_next_work(NOW + timedelta(days=1)) is None


def test_mark_failed_attempt_for_a_symptom_unit(db, store):
    add_processed(db, 1)
    add_symptom_unit(db, 1)
    unit = [store.claim_next_work(NOW) for _ in range(2)][1]
    store.mark_failed_attempt(unit, "S3Unavailable", None)
    assert processing_row(db, 1, "symptom")["status"] == "failed"
    assert stage_of(db, 1) == "failed_symptom"
    assert attempts_of(db, 1, "symptom") == [(1, "failed", "S3Unavailable")]


def batch_unit(db, store):
    for i, age in ((1, 1), (2, 2)):
        add_processed(db, i, source_id=BATCH, age=age)
    store.claim_batch("normal", 2)
    return store.claim_next_work(NOW)


def test_mark_sent_for_a_batch(db, store):
    unit = batch_unit(db, store)
    assert unit.batch_id == "normal_000001"
    store.mark_sent(unit)
    batch = fetch(db, "SELECT status, claimed_at FROM batches")[0]
    assert (batch["status"], batch["claimed_at"]) == ("submitted", None)
    for visit_id in (1, 2):
        assert processing_row(db, visit_id)["status"] == "sent"
        assert stage_of(db, visit_id) == "sent_to_aws"
        assert attempts_of(db, visit_id) == [(1, "sent", None)]
    assert store.claim_next_work(NOW + timedelta(days=1)) is None


def test_mark_failed_attempt_for_a_batch_retry_then_give_up(db, store):
    unit = batch_unit(db, store)
    retry_at = NOW + timedelta(minutes=5)
    store.mark_failed_attempt(unit, "S3Unavailable", retry_at)
    batch = fetch(db, "SELECT * FROM batches")[0]
    assert (batch["status"], batch["attempts"], batch["last_error"], batch["next_retry_at"], batch["claimed_at"]) == (
        "created", 1, "S3Unavailable", retry_at, None)
    assert stage_of(db, 1) == "queued"
    assert attempts_of(db, 1) == [(1, "retry", "S3Unavailable")]
    retried = store.claim_next_work(retry_at)
    assert retried.attempts == 1
    store.mark_failed_attempt(retried, "S3Unavailable", None)
    batch = fetch(db, "SELECT status, attempts, next_retry_at FROM batches")[0]
    assert (batch["status"], batch["attempts"], batch["next_retry_at"]) == ("failed", 2, None)
    for visit_id in (1, 2):
        assert stage_of(db, visit_id) == "failed"
        assert attempts_of(db, visit_id) == [(1, "retry", "S3Unavailable"), (2, "failed", "S3Unavailable")]
    assert store.claim_next_work(NOW + timedelta(days=1)) is None
