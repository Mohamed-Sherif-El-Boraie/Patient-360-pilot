import json
from datetime import UTC, datetime

from patient360.flow.intake import Intake, Parked
from patient360.flow.parked import ParkedRecovery
from tests.fakes import FakeBlobs, FakeStore

SECRET = b"a-long-test-secret-value"
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)


def world():
    store, blobs = FakeStore(), FakeBlobs()
    intake = Intake(store, blobs, SECRET, lambda: NOW)
    return store, blobs, intake, ParkedRecovery(intake, blobs)


def pending(blobs):
    return [name for name in blobs.files if name.startswith("_pending/")]


def test_a_parked_visit_is_saved_once_the_database_is_back_and_its_file_is_removed(sample):
    store, blobs, intake, recovery = world()
    visit = sample("urgent_mild")
    store.fail_on_save = True
    assert intake.receive(json.dumps(visit).encode()) == Parked(visit["visit_id"])
    assert recovery.run() == [] and len(pending(blobs)) == 1          # still down: the file stays
    store.fail_on_save = False
    [done] = recovery.run()
    assert done.outcome == "Accepted" and pending(blobs) == []
    assert visit["visit_id"] in store.visits and store.visits[visit["visit_id"]]["stage"] == "received"


def test_a_parked_visit_that_was_saved_meanwhile_is_only_a_duplicate_and_its_file_is_removed(sample):
    store, blobs, intake, recovery = world()
    visit = sample("urgent_mild")
    body = json.dumps(visit).encode()
    store.fail_on_save = True
    intake.receive(body)
    store.fail_on_save = False
    intake.receive(body)                                  # the sender resent it and it was saved
    [done] = recovery.run()
    assert done.outcome in ("Retry", "Duplicate") and pending(blobs) == []
    assert len(store.visits) == 1


def test_nothing_parked_means_nothing_to_do_and_blob_down_is_not_an_error():
    store, blobs, intake, recovery = world()
    assert recovery.run() == []
    blobs.down = True
    assert recovery.run() == []
