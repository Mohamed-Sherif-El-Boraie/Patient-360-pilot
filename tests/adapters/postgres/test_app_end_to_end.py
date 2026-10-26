"""The whole running service in one process: the real HTTP app, PostgreSQL store, timer cycles and Backend client.

Only the outside world is pretended: Blob and S3 are in memory, and the Backend wrapper is an ``httpx.MockTransport``.
"""
import json
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from patient360.adapters.backend import WrapperBackend
from patient360.config.settings import load_settings
from patient360.http_app import create_app
from patient360.ports import EventMessage
from patient360.runtime import build_services
from patient360.timers import run_job_cycle, run_work_cycle
from tests.adapters.postgres import seed
from tests.fakes import FakeBlobs, FakeS3

VISIT_ID, PATIENT_ID = 2071739799, 2141109993          # the ids of the symptom_check sample
KEY = {"X-Api-Key": "dev-inbound-key"}
ENV = {
    "PGHOST": "127.0.0.1", "PGDATABASE": "patient360", "PGUSER": "app", "PGPASSWORD": "pg-password-xyz",
    "AZURE_STORAGE_CONNECTION_STRING": "UseDevelopmentStorage=true", "BLOB_INPUT_CONTAINER": "ai-input",
    "BLOB_OUTPUT_CONTAINER": "ai-output", "IDEMPOTENCY_KEY_SECRET": "a-long-test-secret-value",
    "INBOUND_API_KEY": "dev-inbound-key",
    "BACKEND_BASE_URL": "https://wrapper.example", "BACKEND_CALLER_SOURCE": "Patient360",
    "BACKEND_CALLER_SOURCE_KEY": "fake-caller-key", "BACKEND_CALLER_USER_NAME": "Patient360",
    "BACKEND_USERNAME": "fake-user", "BACKEND_PASSWORD": "fake-password",
}


def both(english):
    return {"en": english, "ar": f"AR-{english}"}


def answer(with_symptom_keys=False):
    recommendation = {"test_name": "Extra test", "priority": both("important"), "reason": both("because"),
                      "timing": both("soon"), "guideline": both("guide"),
                      "advisory": {"level": both("none"), "message": both("fine")}}
    if with_symptom_keys:
        recommendation |= {"change_type": both("kept"), "explanation": both("still applies")}
    return json.dumps({"summary": both("summary"), "simple_summary": both("simple"), "critical_alerts": [],
                       "note": both("note"), "recommendations": [recommendation]})


class PretendWrapper:
    """The Backend wrapper: a login and a results endpoint, remembering what it was sent."""

    def __init__(self):
        self.logins, self.results = 0, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/Auth/Callerlogin"):
            self.logins += 1
            return httpx.Response(200, json={"response": {"code": -1, "message": "Success"}, "data": {
                "CallerCredential": {"Caller": {"TokenExpiredMin": 480}, "token": "pretend-token"}}})
        assert request.headers["Authorization"] == "Bearer pretend-token"
        self.results.append(json.loads(request.content))
        return httpx.Response(200, json={"response": {"code": -1, "message": "Success"},
                                         "data": {"Patient360Loader": {"ID": len(self.results), "Action": "Insert"}}})


class PretendQueue:
    """The storage queue Event Grid writes to: messages wait in a list and are settled by the consumer."""

    def __init__(self):
        self.waiting, self.settled = [], []

    def receive(self):
        messages, self.waiting = self.waiting, []
        return messages

    def complete(self, message):
        self.settled.append("complete")

    def abandon(self, message):
        self.settled.append("abandon")

    def dead_letter(self, message, reason):
        self.settled.append("dead_letter")

    def close(self):
        pass


class Rig:
    def __init__(self, db, sample):
        self.db, self.sample = db, sample
        self.blob_1, self.blob_2, self.s3, self.wrapper = FakeBlobs(), FakeBlobs(), FakeS3(), PretendWrapper()
        settings = load_settings(ENV)
        seed.add_category(db, 1, "Kidney")
        seed.add_test(db, 900, "Extra test", name_ar="AR-Extra test", description="why", categories=(1,))
        backend = WrapperBackend(settings.backend, 5, 30, transport=httpx.MockTransport(self.wrapper))
        self.queue = PretendQueue()
        self.services = build_services(settings, db=db, blob_stores=(self.blob_1, self.blob_2), s3=self.s3,
                                       backend=backend, event_queue=self.queue)
        self.client = TestClient(create_app(self.services))

    def post(self, document):
        path = "/api/v1/patient360/visits-symptoms" if document["source"] == 3 else "/api/v1/patient360/visits"
        return self.client.post(path, content=json.dumps(document).encode(), headers=KEY)

    def work(self):
        run_work_cycle(self.services, datetime.now(UTC))

    def aws_answers(self, source, text, via_queue=False, **extra):
        """Play AWS and Event Grid: write the answer of the last uploaded file to Blob 2 and tell the app (by the HTTP
        route, or as a message in the storage queue that the app reads)."""
        uploaded_source, s3_key = self.s3.uploads[-1]
        assert uploaded_source == source
        _, lane, year, month, day, name = s3_key.split("/")
        key = name.removesuffix(".json")
        path = f"aws-output-merged/{source}/{lane}/{year}/{month}/{day}/{name}"
        record = {"recordId": key, "model": "model-x", "content": [{"type": "text", "text": text}],
                  "metadata": {"en": {"usage": {"input_tokens": 1, "output_tokens": 2}}}, **extra}
        self.blob_2.files[path] = json.dumps(record).encode()
        event = [{"eventType": "Microsoft.Storage.BlobCreated",
                  "subject": f"/blobServices/default/containers/ai-output/blobs/{path}", "data": {}}]
        if via_queue:
            self.queue.waiting.append(EventMessage(body=json.dumps(event[0]).encode(), handle=path))
            return self.services.event_consumer.run_once()
        return self.client.post("/api/v1/patient360/blob-events", json=event, headers=KEY)


@pytest.fixture
def rig(db, sample):
    return Rig(db, sample)


def test_the_whole_service_from_an_incoming_visit_to_the_delivered_symptom_result(rig):
    visit = rig.sample("urgent_mild") | {"visit_id": VISIT_ID, "patient_id": PATIENT_ID}

    assert rig.client.get("/health").json() == {"status": "ok"}
    assert rig.post(visit).status_code == 202                       # accepted, then processed in the background
    assert rig.post(visit).status_code == 200                       # the same visit again: a duplicate, still success
    rig.work()
    assert [source for source, _ in rig.s3.uploads] == ["urgent"]

    assert rig.aws_answers("urgent", answer()).json() == {"received": 1}
    rig.work()
    [posted] = rig.wrapper.results
    assert (posted["patient_id"], posted["visit_id"], posted["source"]) == (PATIENT_ID, VISIT_ID, 1)
    assert posted["ai"]["summary"] == both("summary") and "AR-Extra test" in posted["html"]["ar"]
    assert "model-x" not in json.dumps(posted) and rig.wrapper.logins == 1

    symptom = rig.sample("symptom_check")
    assert rig.post(symptom).status_code == 202
    rig.work()
    assert [source for source, _ in rig.s3.uploads] == ["urgent", "symptom-checker"]
    assert rig.aws_answers("symptom-checker", answer(with_symptom_keys=True)).status_code == 200
    rig.work()
    assert [r["source"] for r in rig.wrapper.results] == [1, 3]
    assert rig.wrapper.results[1]["ai"]["recommendations"][0]["change_type"] == both("kept")
    assert rig.wrapper.logins == 1                                  # one login for everything: the token is reused

    [visit_row] = seed.fetch(rig.db, "SELECT stage, tier FROM visits")
    assert visit_row["stage"] == "delivered_symptom" and visit_row["tier"] == "MILD"
    statuses = seed.fetch(rig.db, "SELECT analysis, status FROM record_processing ORDER BY analysis")
    assert [(r["analysis"], r["status"]) for r in statuses] == [("symptom", "delivered"), ("visit", "delivered")]


def test_the_answer_file_can_arrive_through_the_storage_queue_instead_of_http(rig):
    visit = rig.sample("urgent_mild") | {"visit_id": VISIT_ID, "patient_id": PATIENT_ID}
    assert rig.post(visit).status_code == 202
    rig.work()

    assert rig.aws_answers("urgent", answer(), via_queue=True) == 1            # one message read
    assert rig.queue.settled == ["complete"]
    rig.work()
    [posted] = rig.wrapper.results
    assert (posted["visit_id"], posted["source"]) == (VISIT_ID, 1) and posted["ai"]["summary"] == both("summary")

    rig.queue.waiting.append(EventMessage(body=b"garbage", handle="bad"))        # a message that is not an event
    assert rig.services.event_consumer.run_once() == 1 and rig.queue.settled == ["complete", "dead_letter"]


def test_a_symptom_check_is_refused_with_409_when_the_visit_result_closed_the_checker(rig):
    visit = rig.sample("urgent_mild") | {"visit_id": VISIT_ID, "patient_id": PATIENT_ID}
    assert rig.post(visit).status_code == 202
    rig.work()
    blocked = {"amazon-bedrock-guardrailAction": "INTERVENED"}
    assert rig.aws_answers("urgent", answer(), **blocked).status_code == 200
    rig.work()
    assert rig.wrapper.results[0]["symptom_flag"] is False                  # the Backend was told: no symptom box
    reply = rig.post(rig.sample("symptom_check"))
    assert reply.status_code == 409
    assert reply.json() == {"response": {"code": 1, "message": "SYMPTOM_CHECKER_CLOSED"}}
    assert seed.fetch(rig.db, "SELECT symptom_received_at FROM visits")[0]["symptom_received_at"] is None
    assert [source for source, _ in rig.s3.uploads] == ["urgent"]            # nothing was processed or uploaded


def test_a_wrong_or_missing_api_key_is_refused_and_nothing_is_saved(rig):
    visit = rig.sample("urgent_mild")
    body = json.dumps(visit).encode()
    assert rig.client.post("/api/v1/patient360/visits", content=body).status_code == 401
    assert rig.client.post("/api/v1/patient360/visits", content=body, headers={"X-Api-Key": "wrong"}).status_code == 401
    assert seed.fetch(rig.db, "SELECT count(*) AS n FROM visits")[0]["n"] == 0


def test_an_invalid_visit_is_refused_with_422_and_kept_in_the_rejected_table(rig):
    broken = rig.sample("urgent_mild") | {"patient_id": "not a number"}
    reply = rig.post(broken)
    assert reply.status_code == 422 and "not a number" not in reply.text
    assert seed.fetch(rig.db, "SELECT count(*) AS n FROM rejected_payloads")[0]["n"] == 1


def test_a_visit_cut_short_by_a_restart_is_picked_up_by_the_job_cycle(rig):
    visit = rig.sample("urgent_mild") | {"visit_id": VISIT_ID, "patient_id": PATIENT_ID}
    rig.services.intake.receive(json.dumps(visit).encode())          # accepted, but nobody processed it
    assert seed.fetch(rig.db, "SELECT stage FROM visits")[0]["stage"] == "received"
    run_job_cycle(rig.services, datetime(2999, 1, 1, tzinfo=UTC))   # far enough in the future to count as "stuck"
    assert seed.fetch(rig.db, "SELECT stage FROM visits")[0]["stage"] == "processed"
