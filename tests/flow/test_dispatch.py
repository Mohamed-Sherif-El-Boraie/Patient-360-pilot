import json
from datetime import UTC, datetime, timedelta

import pytest

from patient360.flow.batching import BatchTrigger
from patient360.flow.dispatch import Dispatcher
from patient360.flow.intake import Intake
from patient360.flow.process import Processor
from tests.fakes import FakeBlobs, FakeCatalogue, FakeS3, FakeStore

SECRET = b"a-long-test-secret-value"
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)


class World:
    """The in-memory stand-ins and a clock the test can move."""

    def __init__(self, sample):
        self.sample, self.store, self.blobs, self.s3 = sample, FakeStore(), FakeBlobs(), FakeS3()
        self.clock = NOW

    def add(self, name, visit_id, count=1):
        """Receive and process ``count`` visits of a sample, with consecutive ids; returns the last visit."""
        intake = Intake(self.store, self.blobs, SECRET, lambda: NOW)
        processor = Processor(self.store, FakeCatalogue(), self.blobs)
        for i in range(count):
            visit = self.sample(name)
            visit.update(visit_id=visit_id + i, patient_id=7_000_000 + visit_id + i)
            intake.receive(json.dumps(visit).encode())
            processor.process(visit_id + i)
        return self.store.visits[visit_id + count - 1]

    def dispatcher(self):
        return Dispatcher(self.store, self.blobs, self.s3, lambda: self.clock)


@pytest.fixture
def world(sample):
    return World(sample)


def test_an_urgent_visit_goes_to_its_s3_bucket_with_the_same_bytes_as_blob(world):
    visit = world.add("urgent_mild", 1)
    key = visit["idempotency_key"]
    [done] = world.dispatcher().run()
    assert done.outcome == "sent" and visit["stage"] == "sent_to_aws"
    s3_key = f"processed/normal/2026/10/24/00-input/{key}.json"
    assert world.s3.files[("urgent", s3_key)] == world.blobs.files[f"processed/urgent/normal/2026/10/24/{key}.json"]


def test_the_order_is_urgent_then_symptom_checker_then_on_demand_then_batch(world):
    world.add("batch_normal", 100, count=3)               # the oldest work is the batch ...
    BatchTrigger(world.store, world.blobs, 3).run()
    world.add("on_demand_critical", 200)
    urgent = world.add("urgent_mild", 300)                # ... and the urgent visit is the newest
    world.store.add_symptom_check_ready(300, urgent["model_input"])
    done = world.dispatcher().run()
    assert [d.unit.source for d in done] == ["urgent", "symptom-checker", "on-demand", "batch"]
    assert [source for source, _ in world.s3.uploads] == ["urgent", "symptom-checker", "on-demand", "batch"]
    assert world.store.symptom_checks[300]["stage"] == "sent_to_aws_symptom"


def test_a_unit_is_claimed_by_one_worker_only(world):
    world.add("urgent_mild", 1)
    first = world.store.claim_next_work(NOW)
    assert first is not None and world.store.claim_next_work(NOW) is None
    assert world.dispatcher().run() == []          # claimed by someone else: this dispatcher leaves it alone


def test_a_batch_uploads_its_jsonl_and_the_whole_batch_moves_on(world):
    world.add("batch_normal", 100, count=3)
    [batch] = BatchTrigger(world.store, world.blobs, 3).run()
    [done] = world.dispatcher().run()
    assert done.unit.batch_id == batch.batch_id and world.store.batches[batch.batch_id]["status"] == "submitted"
    assert {v["stage"] for v in world.store.visits.values()} == {"sent_to_aws"}
    jsonl_path = f"processed/batch/{batch.lane}-jsonls/2026/10/25/{batch.batch_id}.jsonl"
    s3_batch_key = f"processed/{batch.lane}/2026/10/25/{batch.batch_id}/00-input/{batch.batch_id}.jsonl"
    assert world.s3.files[("batch", s3_batch_key)] \
        == world.blobs.files[jsonl_path]


@pytest.mark.parametrize("how", ["file_missing", "blob_down"])
def test_a_lost_jsonl_is_rebuilt_from_the_visits_in_the_database(world, how):
    world.add("batch_normal", 100, count=3)
    [batch] = BatchTrigger(world.store, world.blobs, 3).run()
    jsonl_path = f"processed/batch/{batch.lane}-jsonls/2026/10/25/{batch.batch_id}.jsonl"
    original = world.blobs.files.pop(jsonl_path)
    world.blobs.down = how == "blob_down"
    world.dispatcher().run()
    s3_key = ("batch", f"processed/{batch.lane}/2026/10/25/{batch.batch_id}/00-input/{batch.batch_id}.jsonl")
    assert world.s3.files[s3_key] == original
    if how == "file_missing":
        assert world.blobs.files[jsonl_path] == original     # and it is put back in Blob


def test_a_visit_saved_only_in_the_database_is_sent_from_there_and_put_back_in_blob(world):
    world.blobs.down = True
    visit = world.add("urgent_mild", 1)
    assert visit["processed_read_from"] == "postgresql"
    world.blobs.down = False
    world.dispatcher().run()
    path = f"processed/urgent/normal/2026/10/24/{visit['idempotency_key']}.json"
    assert world.s3.uploads and world.blobs.files[path] == next(iter(world.s3.files.values()))


def test_a_missing_blob_file_falls_back_to_the_database_copy(world):
    visit = world.add("urgent_mild", 1)
    world.blobs.files.clear()
    world.dispatcher().run()
    sent = json.loads(next(iter(world.s3.files.values())))
    assert sent == {"recordId": visit["idempotency_key"], "modelInput": visit["model_input"]}


def test_a_failed_upload_waits_longer_each_time_and_after_five_tries_gives_up(world):
    visit = world.add("urgent_mild", 1)
    world.s3.down = True
    waits = []
    for expected in ("retry", "retry", "retry", "retry", "failed"):
        [done] = world.dispatcher().run()
        assert done.outcome == expected
        waits.append(visit["next_retry_at"] and visit["next_retry_at"] - world.clock)
        assert world.dispatcher().run() == []                         # not before its time
        world.clock += timedelta(hours=1)
    assert waits == [timedelta(seconds=s) for s in (10, 30, 120, 600)] + [None]
    assert visit["stage"] == "failed" and visit["attempts"] == 5 and visit["last_error"] == "S3Unavailable"
    world.s3.down = False
    assert world.dispatcher().run() == [] and not world.s3.uploads    # a failed visit is not tried again


def test_an_upload_that_works_on_the_second_try_is_sent(world):
    visit = world.add("urgent_mild", 1)
    world.s3.down = True
    assert world.dispatcher().run()[0].outcome == "retry"
    world.s3.down = False
    world.clock += timedelta(seconds=10)
    assert world.dispatcher().run()[0].outcome == "sent" and visit["stage"] == "sent_to_aws"
