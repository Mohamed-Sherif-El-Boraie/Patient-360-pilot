"""The four parts together, against a real PostgreSQL: the whole flow of the service with the real store.

Blob, S3 and the Backend are still the in-memory fakes (their real versions come later).
"""
import inspect
import json
from datetime import UTC, date, datetime, timedelta

import pytest

from patient360.adapters.postgres.store import PostgresCatalogue, PostgresStore
from patient360.domain.common.blob_paths import merged_output_jsonl_path, merged_output_path
from patient360.flow.batching import BatchTrigger
from patient360.flow.dispatch import Dispatcher
from patient360.flow.intake import Accepted, Duplicate, Intake
from patient360.flow.process import Processor, process_stuck_visits
from patient360.flow.results import OutputReceiver
from patient360.flow.send_result import ResultSender
from patient360.flow.symptom import SymptomProcessor, process_waiting_symptoms
from patient360.ports import Catalogue, VisitStore
from tests.adapters.postgres import seed
from tests.fakes import FakeBackend, FakeBlobs, FakeS3, FunctionApp

SECRET = b"a-long-test-secret-value"
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
VISIT_ID, PATIENT_ID = 2071739799, 2141109993          # the ids of the symptom_check sample
DAY = date(2026, 10, 24)


def both(english):
    return {"en": english, "ar": f"AR-{english}"}


def answer_text(with_symptom_keys=False):
    recommendation = {"test_name": "Extra test", "priority": both("important"), "reason": both("because"),
                      "timing": both("soon"), "guideline": both("guide"),
                      "advisory": {"level": both("none"), "message": both("fine")}}
    if with_symptom_keys:
        recommendation |= {"change_type": both("kept"), "explanation": both("still applies")}
    return json.dumps({"summary": both("summary"), "simple_summary": both("simple"), "critical_alerts": [],
                       "note": both("note"), "recommendations": [recommendation]})


def aws_record(key, text, **extra):
    return {"recordId": key, "type": "message", "role": "assistant", "model": "model-x",
            "content": [{"type": "text", "text": text}],
            "metadata": {"en": {"stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 20}},
                         "ar": {"stop_reason": "end_turn", "usage": {"input_tokens": 5, "output_tokens": 6}}}, **extra}


class World:
    """The real PostgreSQL store and catalogue with the real flow classes on top."""

    def __init__(self, db, sample):
        self.db, self.sample = db, sample
        self.store, self.catalogue = PostgresStore(db), PostgresCatalogue(db)
        self.blobs, self.s3, self.backend = FakeBlobs(), FakeS3(), FakeBackend()
        self.out = {source: FakeBlobs() for source in ("urgent", "symptom-checker", "on-demand", "batch")}
        self.clock = NOW
        seed.add_category(db, 1, "Kidney")
        seed.add_test(db, 900, "Extra test", name_ar="AR-Extra test", description="why", categories=(1,))

    def intake(self):
        return Intake(self.store, self.blobs, SECRET, lambda: NOW)

    def receive_visit(self, name, visit_id=VISIT_ID, patient_id=PATIENT_ID):
        visit = self.sample(name)
        visit.update(visit_id=visit_id, patient_id=patient_id)
        outcome = self.intake().receive(json.dumps(visit).encode())
        assert isinstance(outcome, Accepted)
        self.mark_every_parameter_as_kidney(visit)
        return outcome.idempotency_key

    def mark_every_parameter_as_kidney(self, visit):
        for test in visit["tests"]:
            for result in test["results"]:
                with self.db.transaction() as conn:
                    conn.execute("UPDATE parameters SET category_id = ARRAY[1], active = true WHERE parameter_id = %s",
                                 (result["parameter_id"],))

    def process(self, visit_id=VISIT_ID):
        return Processor(self.store, self.catalogue, self.blobs).process(visit_id)

    def dispatch(self):
        return Dispatcher(self.store, self.blobs, self.s3, lambda: self.clock).run()

    def receiver(self):
        return FunctionApp(OutputReceiver(self.store, self.catalogue), self.out)

    def aws_answers(self, source, path, *records):
        self.out[source].files[path] = "\n".join(json.dumps(r) for r in records).encode()
        return self.receiver().receive(source, path)

    def send(self):
        return ResultSender(self.store, self.backend, lambda: self.clock).run()


@pytest.fixture
def world(db, sample):
    return World(db, sample)


def test_the_store_has_every_method_of_the_port_with_the_same_parameters():
    for port, real in ((VisitStore, PostgresStore), (Catalogue, PostgresCatalogue)):
        methods = [(name, method) for name, method in inspect.getmembers(port, inspect.isfunction)
                   if not name.startswith("_")]
        assert methods
        for name, port_method in methods:
            assert hasattr(real, name), f"{real.__name__}.{name} is missing"
            assert list(inspect.signature(getattr(real, name)).parameters) == \
                list(inspect.signature(port_method).parameters), name


def test_an_urgent_visit_goes_from_intake_to_the_backend(world):
    key = world.receive_visit("urgent_mild")
    done = world.process()
    assert (done.tier, done.lane, done.read_from) == ("MILD", "normal", "blob")
    [sent] = world.dispatch()
    assert sent.outcome == "sent" and [source for source, _ in world.s3.uploads] == ["urgent"]
    path = merged_output_path("normal", DAY, key)
    assert [r.outcome for r in world.aws_answers("urgent", path, aws_record(key, answer_text()))] == ["saved"]
    [delivered] = world.send()
    assert delivered.outcome == "delivered"
    [posted] = world.backend.posts
    assert (posted["patient_id"], posted["visit_id"], posted["source"]) == (PATIENT_ID, VISIT_ID, 1)
    assert posted["ai"]["recommendations"][0]["test_name"] == "Extra test"
    assert "AR-Extra test" in posted["html"]["ar"] and "model-x" not in json.dumps(posted)
    [visit] = seed.fetch(world.db, "SELECT stage, tier, lane FROM visits")
    assert (visit["stage"], visit["tier"], visit["lane"]) == ("delivered", "MILD", "normal")
    assert seed.fetch(world.db, "SELECT status FROM record_processing")[0]["status"] == "delivered"


def test_a_duplicate_visit_is_answered_as_a_duplicate(world):
    world.receive_visit("urgent_mild")
    outcome = world.intake().receive(json.dumps(world.sample("urgent_mild") | {
        "visit_id": VISIT_ID, "patient_id": PATIENT_ID}).encode())
    assert outcome == Duplicate(VISIT_ID)


def test_a_batch_is_made_uploaded_answered_and_every_visit_delivered(world):
    keys = [world.receive_visit("batch_normal", 100 + i, 7_000_000 + i) for i in range(3)]
    for i in range(3):
        world.process(100 + i)
    [batch] = BatchTrigger(world.store, world.blobs, 3).run()
    [sent] = world.dispatch()
    assert sent.unit.batch_id == batch.batch_id and [s for s, _ in world.s3.uploads] == ["batch"]
    assert seed.fetch(world.db, "SELECT status FROM batches")[0]["status"] == "submitted"
    jsonl_path = merged_output_jsonl_path(batch.lane, date(2026, 10, 25), batch.batch_id)
    first = OutputReceiver(world.store, world.catalogue).receive_record(
        "batch", jsonl_path, json.dumps(aws_record(keys[0], answer_text())))
    assert [r.outcome for r in first] == ["saved"]
    assert seed.fetch(world.db, "SELECT status FROM batches")[0]["status"] == "partially_completed"   # 1 of 3 so far
    results = world.aws_answers("batch", jsonl_path, *[aws_record(key, answer_text()) for key in keys])
    assert [r.outcome for r in results] == ["duplicate", "saved", "saved"]
    assert seed.fetch(world.db, "SELECT status FROM batches")[0]["status"] == "completed"
    assert [d.outcome for d in world.send()] == ["delivered"] * 3
    assert sorted(post["visit_id"] for post in world.backend.posts) == [100, 101, 102]
    assert {r["mode"] for r in seed.fetch(world.db, "SELECT mode FROM ai_model_calls")} == {"batch_inference"}


def test_the_symptom_check_runs_its_own_way_after_the_visit_result(world):
    key = world.receive_visit("urgent_mild")
    world.process()
    world.dispatch()
    world.aws_answers("urgent", merged_output_path("normal", DAY, key), aws_record(key, answer_text()))
    world.send()
    symptom = world.sample("symptom_check")
    assert isinstance(world.intake().receive(json.dumps(symptom).encode()), Accepted)
    done = SymptomProcessor(world.store, world.blobs).process(VISIT_ID)
    assert (done.lane, done.read_from) == ("normal", "blob")
    [sent] = world.dispatch()
    assert sent.unit.analysis == "symptom" and world.s3.uploads[-1][0] == "symptom-checker"
    path = merged_output_path("normal", DAY, key)
    record = aws_record(key, answer_text(with_symptom_keys=True))
    assert [r.outcome for r in world.aws_answers("symptom-checker", path, record)] == ["saved"]
    assert [d.outcome for d in world.send()] == ["delivered"]
    visit_post, symptom_post = world.backend.posts
    assert (visit_post["source"], symptom_post["source"]) == (1, 3)
    assert symptom_post["ai"]["recommendations"][0]["change_type"] == both("kept")
    assert {r["analysis"] for r in seed.fetch(world.db, "SELECT analysis FROM ai_outputs")} == {"visit", "symptom"}


def test_a_blocked_result_is_delivered_with_the_fixed_message(world):
    key = world.receive_visit("urgent_mild")
    world.process()
    world.dispatch()
    blocked = aws_record(key, answer_text(), **{"amazon-bedrock-guardrailAction": "INTERVENED"})
    assert [r.outcome for r in world.aws_answers(
        "urgent", merged_output_path("normal", DAY, key), blocked)] == ["blocked"]
    world.send()
    assert world.backend.posts[0]["ai"]["recommendations"] == []
    assert seed.fetch(world.db, "SELECT status FROM ai_outputs")[0]["status"] == "guardrail_blocked"


def test_sweeps_pick_up_a_visit_and_a_symptom_check_that_nobody_processed(world):
    world.receive_visit("urgent_mild")
    later = NOW + timedelta(minutes=10)
    [done] = process_stuck_visits(world.store, Processor(world.store, world.catalogue, world.blobs), later)
    assert done.visit_id == VISIT_ID
    assert process_stuck_visits(world.store, Processor(world.store, world.catalogue, world.blobs), later) == []
    world.intake().receive(json.dumps(world.sample("symptom_check")).encode())
    symptom_processor = SymptomProcessor(world.store, world.blobs)
    assert process_waiting_symptoms(world.store, symptom_processor, later) == []       # the visit has no result yet
    assert seed.fetch(world.db, "SELECT count(*) AS n FROM record_processing WHERE analysis = 'symptom'")[0]["n"] == 0


def test_a_failed_upload_and_a_failed_delivery_are_retried_later(world):
    key = world.receive_visit("urgent_mild")
    world.process()
    world.s3.down = True
    assert [d.outcome for d in world.dispatch()] == ["retry"]
    assert world.dispatch() == []                                       # not before its time
    world.s3.down, world.clock = False, NOW + timedelta(seconds=10)
    assert [d.outcome for d in world.dispatch()] == ["sent"]
    world.aws_answers("urgent", merged_output_path("normal", DAY, key), aws_record(key, answer_text()))
    from patient360.ports import BackendUnavailable
    world.backend.raises = BackendUnavailable
    assert [d.outcome for d in world.send()] == ["retry"]
    world.backend.raises, world.clock = None, world.clock + timedelta(seconds=10)
    assert [d.outcome for d in world.send()] == ["delivered"]
