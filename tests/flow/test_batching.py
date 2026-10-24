import json
from datetime import UTC, datetime

import pytest

from patient360.flow.batching import BatchTrigger
from patient360.flow.intake import Intake
from patient360.flow.process import Processor
from tests.fakes import FakeBlobs, FakeCatalogue, FakeStore

SECRET = b"a-long-test-secret-value"
RECEIVED = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
TICK = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)   # the next day: the batch folder uses the day of the batch


@pytest.fixture
def parts():
    return FakeStore(), FakeBlobs()


def add_visits(sample, store, blobs, name, first_visit_id, count):
    """Receive and process ``count`` copies of a sample, each with its own patient and visit id."""
    intake = Intake(store, blobs, SECRET, lambda: RECEIVED)
    processor = Processor(store, FakeCatalogue(), blobs)
    for i in range(count):
        visit = sample(name)
        visit.update(visit_id=first_visit_id + i, patient_id=7_000_000 + first_visit_id + i)
        intake.receive(json.dumps(visit).encode())
        processor.process(visit["visit_id"])


def trigger(store, blobs, size=3):
    return BatchTrigger(store, blobs, lambda: TICK, size)


def test_fewer_than_a_full_batch_creates_nothing(sample, parts):
    store, blobs = parts
    add_visits(sample, store, blobs, "batch_normal", 100, 2)
    assert trigger(store, blobs).run() == []
    assert all(v["stage"] == "processed" for v in store.visits.values())


def test_a_full_batch_writes_one_file_per_visit_and_the_jsonl(sample, parts):
    store, blobs = parts
    add_visits(sample, store, blobs, "batch_normal", 100, 3)
    [batch] = trigger(store, blobs).run()
    lane = batch.lane
    assert batch.batch_id == f"{lane}_000001" and batch.read_from == "blob"
    keys = [v["idempotency_key"] for v in store.visits.values()]
    for key in keys:
        assert f"processed/batch/{lane}/2026/10/25/{batch.batch_id}/{key}.json" in blobs.files
    jsonl_path = f"processed/batch/{lane}-jsonls/2026/10/25/{batch.batch_id}.jsonl"
    lines = [json.loads(line) for line in blobs.files[jsonl_path].decode().splitlines()]
    assert sorted(line["recordId"] for line in lines) == sorted(keys) and blobs.files[jsonl_path] == batch.jsonl
    assert all(set(line) == {"recordId", "document"} for line in lines)
    assert all(v["stage"] == "queued" and v["batch_id"] == batch.batch_id for v in store.visits.values())


def test_the_default_batch_is_100_visits(sample, parts):
    store, blobs = parts
    add_visits(sample, store, blobs, "batch_normal", 1000, 99)
    assert BatchTrigger(store, blobs, lambda: TICK).run() == []
    add_visits(sample, store, blobs, "batch_normal", 2000, 1)
    [batch] = BatchTrigger(store, blobs, lambda: TICK).run()
    assert len(batch.jsonl.decode().splitlines()) == 100


def test_a_visit_is_never_batched_twice_and_the_rest_wait(sample, parts):
    store, blobs = parts
    add_visits(sample, store, blobs, "batch_normal", 100, 7)
    first = trigger(store, blobs).run()
    assert [b.batch_id[-6:] for b in first] == ["000001", "000002"]       # 7 visits: two batches of 3, one waits
    assert trigger(store, blobs).run() == []
    waiting = [v for v in store.visits.values() if v["stage"] == "processed"]
    assert len(waiting) == 1


def test_the_two_lanes_batch_separately_with_their_own_numbers(sample, parts):
    store, blobs = parts
    add_visits(sample, store, blobs, "batch_normal", 100, 3)
    add_visits(sample, store, blobs, "batch_critical", 200, 3)
    lanes = {b.lane: b.batch_id for b in trigger(store, blobs).run()}
    assert lanes == {"normal": "normal_000001", "critical": "critical_000001"}


def test_urgent_and_on_demand_visits_are_never_batched(sample, parts):
    store, blobs = parts
    add_visits(sample, store, blobs, "urgent_mild", 100, 3)
    assert trigger(store, blobs).run() == []


def test_when_blob_is_down_the_batch_is_still_made_and_read_from_the_database(sample, parts):
    store, blobs = parts
    add_visits(sample, store, blobs, "batch_normal", 100, 3)
    blobs.down = True
    [batch] = trigger(store, blobs).run()
    assert batch.read_from == "postgresql" and len(batch.jsonl.decode().splitlines()) == 3
    assert all(v["processed_read_from"] == "postgresql" for v in store.visits.values())
