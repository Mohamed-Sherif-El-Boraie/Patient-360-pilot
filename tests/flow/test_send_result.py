import json
from datetime import UTC, datetime, timedelta

import pytest

from patient360.flow.intake import Intake
from patient360.flow.process import Processor
from patient360.flow.send_result import ResultSender
from patient360.ports import BackendRejected, BackendUnavailable
from tests.fakes import FakeBackend, FakeBlobs, FakeCatalogue, FakeStore

SECRET = b"a-long-test-secret-value"
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)


class World:
    def __init__(self, sample):
        self.sample, self.store, self.backend = sample, FakeStore(), FakeBackend()
        self.blobs, self.clock = FakeBlobs(), NOW

    def add(self, name, visit_id):
        """A visit whose result is ready to send; returns its record and the body it will send."""
        visit = self.sample(name)
        visit.update(visit_id=visit_id, patient_id=7_000_000 + visit_id)
        Intake(self.store, self.blobs, SECRET, lambda: NOW).receive(json.dumps(visit).encode())
        Processor(self.store, FakeCatalogue(), self.blobs).process(visit_id)
        body = {"source": visit["source"], "visit_id": visit_id, "note": "result"}
        self.store.add_result_ready(visit_id, body)
        return self.store.visits[visit_id], body

    def sender(self):
        return ResultSender(self.store, self.backend, lambda: self.clock)


@pytest.fixture
def world(sample):
    return World(sample)


def test_a_ready_result_is_sent_once_exactly_as_saved(world):
    visit, body = world.add("urgent_mild", 1)
    [done] = world.sender().run()
    assert done.outcome == "delivered" and visit["stage"] == "delivered"
    assert world.backend.posts == [body]
    assert world.sender().run() == [] and len(world.backend.posts) == 1


def test_results_are_sent_urgent_then_symptom_checker_then_on_demand_then_batch(world):
    world.add("batch_normal", 100)
    world.add("on_demand_critical", 200)
    world.add("urgent_mild", 300)
    world.store.symptom_checks[300] = {"received_at": NOW}
    world.store.add_result_ready(300, {"source": 3, "visit_id": 300}, analysis="symptom")
    world.sender().run()
    assert [post["source"] for post in world.backend.posts] == [1, 3, 4, 2]
    assert world.store.symptom_checks[300]["stage"] == "delivered_symptom"


def test_a_busy_backend_is_retried_with_growing_waits_and_after_five_tries_the_result_fails(world):
    visit, _ = world.add("urgent_mild", 1)
    world.backend.raises = BackendUnavailable
    waits = []
    for expected in ("retry", "retry", "retry", "retry", "failed"):
        [done] = world.sender().run()
        assert done.outcome == expected
        waits.append(visit["delivery_retry_at"] and visit["delivery_retry_at"] - world.clock)
        assert world.sender().run() == []                              # not before its time
        world.clock += timedelta(hours=1)
    assert waits == [timedelta(seconds=s) for s in (10, 30, 120, 600)] + [None]
    assert visit["stage"] == "failed" and visit["delivery_attempts"] == 5
    assert visit["delivery_error"] == "BackendUnavailable"
    world.backend.raises = None
    assert world.sender().run() == [] and not world.backend.posts      # a failed result is not tried again


def test_a_result_the_backend_refuses_is_not_retried(world):
    visit, _ = world.add("urgent_mild", 1)
    world.backend.raises = BackendRejected
    [done] = world.sender().run()
    assert done.outcome == "failed" and visit["stage"] == "failed" and visit["delivery_attempts"] == 1


def test_a_result_sent_on_the_second_try_is_delivered(world):
    visit, body = world.add("urgent_mild", 1)
    world.backend.raises = BackendUnavailable
    assert world.sender().run()[0].outcome == "retry"
    world.backend.raises = None
    world.clock += timedelta(seconds=10)
    assert world.sender().run()[0].outcome == "delivered" and world.backend.posts == [body]
    assert visit["stage"] == "delivered"


def test_one_failing_result_does_not_hold_back_the_next(world):
    first, _ = world.add("urgent_mild", 1)
    _, second_body = world.add("on_demand_critical", 2)
    sent = []

    class FailsForTheUrgentOne(FakeBackend):
        def post_result(self, body):
            if body["visit_id"] == 1:
                raise BackendUnavailable
            sent.append(body)

    ResultSender(world.store, FailsForTheUrgentOne(), lambda: world.clock).run()
    assert sent == [second_body] and first["stage"] == "post_processing"
