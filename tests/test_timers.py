import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

from structlog.testing import capture_logs

from patient360 import timers
from patient360.config.settings import load_settings
from patient360.flow.batching import BatchTrigger
from patient360.flow.dispatch import Dispatcher
from patient360.flow.intake import Intake
from patient360.flow.process import Processor
from patient360.flow.results import OutputReceiver
from patient360.flow.send_result import ResultSender
from patient360.flow.symptom import SymptomProcessor
from patient360.services import Services
from tests.fakes import FakeBackend, FakeBlobs, FakeCatalogue, FakeS3, FakeStore
from tests.flow.test_symptom import NOW, SECRET, VISIT_ID, World

SETTINGS = load_settings({
    "PGHOST": "h", "PGDATABASE": "d", "PGUSER": "u", "PGPASSWORD": "p", "AZURE_STORAGE_CONNECTION_STRING": "c",
    "BLOB_INPUT_CONTAINER": "in", "IDEMPOTENCY_KEY_SECRET": "x" * 20,
})
LATER = NOW + timedelta(minutes=6)


class SpyDb:
    closed = False

    def close(self):
        self.closed = True


class CountingBatchTrigger:
    calls = 0

    def run(self):
        self.calls += 1
        return []


class BrokenProcessor(Processor):
    def process(self, visit_id):
        raise RuntimeError(f"patient {visit_id}")


def make_services(store, blobs, s3, catalogue, backend=None):
    return Services(
        settings=SETTINGS, db=SpyDb(), store=store, intake=Intake(store, blobs, SECRET, lambda: NOW),
        processor=Processor(store, catalogue, blobs), symptom_processor=SymptomProcessor(store, blobs),
        batch_trigger=BatchTrigger(store, blobs, 100), dispatcher=Dispatcher(store, blobs, s3, lambda: NOW),
        receiver=OutputReceiver(store, catalogue),
        sender=ResultSender(store, backend, lambda: NOW) if backend else None)


def services_of(world, backend=None):
    return make_services(world.store, world.blobs, world.s3, world.catalogue, backend)


def test_the_job_cycle_processes_a_stuck_visit(sample):
    store, blobs = FakeStore(), FakeBlobs()
    visit = sample("urgent_mild")
    Intake(store, blobs, SECRET, lambda: NOW).receive(json.dumps(visit).encode())
    services = make_services(store, blobs, FakeS3(), FakeCatalogue())
    timers.run_job_cycle(services, LATER)
    assert store.visits[visit["visit_id"]]["stage"] == "processed"


def test_the_job_cycle_processes_a_waiting_symptom_check_and_runs_the_batch_trigger(sample):
    world = World(sample)
    world.give_the_visit_its_result()
    world.receive_the_symptom_check()
    spy = CountingBatchTrigger()
    services = replace(services_of(world), batch_trigger=spy)
    timers.run_job_cycle(services, LATER)
    assert world.store.symptom_checks[VISIT_ID]["stage"] == "processed_symptom" and spy.calls == 1


def test_a_failing_job_is_logged_without_data_and_does_not_stop_the_next(sample):
    store, blobs = FakeStore(), FakeBlobs()
    Intake(store, blobs, SECRET, lambda: NOW).receive(json.dumps(sample("urgent_mild")).encode())
    spy = CountingBatchTrigger()
    services = replace(make_services(store, blobs, FakeS3(), FakeCatalogue()),
                       processor=BrokenProcessor(store, FakeCatalogue(), blobs), batch_trigger=spy)
    with capture_logs() as logs:
        timers.run_job_cycle(services, LATER)
    assert spy.calls == 1
    assert logs == [{"event": "job_failed", "error_type": "RuntimeError", "log_level": "warning"}]


def test_the_work_cycle_uploads_and_delivers(sample):
    uploading, delivering = World(sample), World(sample)
    delivering.give_the_visit_its_result()
    backend = FakeBackend()
    timers.run_work_cycle(services_of(uploading, backend), NOW)
    timers.run_work_cycle(services_of(delivering, backend), NOW)
    assert [source for source, _ in uploading.s3.uploads] == ["urgent"] and len(backend.posts) == 1


def test_the_work_cycle_without_a_backend_only_uploads(sample):
    world = World(sample)
    timers.run_work_cycle(services_of(world), NOW)
    assert [source for source, _ in world.s3.uploads] == ["urgent"]


def test_the_lifespan_runs_both_loops_and_stops_them(monkeypatch):
    calls = {"job": 0, "work": 0}
    monkeypatch.setattr(timers, "run_job_cycle", lambda services, now: calls.__setitem__("job", calls["job"] + 1))
    monkeypatch.setattr(timers, "run_work_cycle", lambda services, now: calls.__setitem__("work", calls["work"] + 1))
    db = SpyDb()
    services = SimpleNamespace(settings=SimpleNamespace(job_interval_s=0.01, work_interval_s=0.01), db=db)

    async def scenario():
        async with timers.lifespan(services)(None):
            await asyncio.sleep(0.2)
        return [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]

    with capture_logs() as logs:
        leftover = asyncio.run(scenario())
    assert calls["job"] >= 2 and calls["work"] >= 2 and leftover == [] and db.closed
    assert logs == []
