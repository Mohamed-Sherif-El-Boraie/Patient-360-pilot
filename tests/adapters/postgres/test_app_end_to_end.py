"""The whole running service in one process: the real HTTP app, PostgreSQL store, timer cycles and Backend client.

Only the outside world is pretended: Blob and S3 are in memory, and the Backend wrapper is an ``httpx.MockTransport``.
"""
import json
from datetime import UTC, date, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from patient360.adapters.backend import WrapperBackend
from patient360.config.settings import load_settings
from patient360.domain.common.blob_paths import merged_output_path
from patient360.http_app import create_app
from patient360.runtime import build_services
from patient360.timers import run_job_cycle, run_work_cycle
from tests.adapters.postgres import seed
from tests.fakes import FakeBlobs, FakeS3

VISIT_ID, PATIENT_ID = 2071739799, 2141109993          # the ids of the symptom_check sample
KEY = {"X-Api-Key": "dev-inbound-key"}
ENV = {
    "PGHOST": "127.0.0.1", "PGDATABASE": "patient360", "PGUSER": "app", "PGPASSWORD": "pg-password-xyz",
    "AZURE_STORAGE_CONNECTION_STRING": "UseDevelopmentStorage=true", "BLOB_INPUT_CONTAINER": "ai-input",
    "IDEMPOTENCY_KEY_SECRET": "a-long-test-secret-value",
    "INBOUND_API_KEY": "dev-inbound-key",
    "BACKEND_BASE_URL": "https://wrapper.example", "BACKEND_CALLER_SOURCE": "Patient360",
    "BACKEND_CALLER_SOURCE_KEY": "fake-caller-key", "BACKEND_CALLER_USER_NAME": "Patient360",
    "BACKEND_USERNAME": "fake-user", "BACKEND_PASSWORD": "fake-password",
    "S3_BUCKET_URGENT": "p360-urgent", "S3_BUCKET_ON_DEMAND": "p360-on-demand",
    "S3_BUCKET_SYMPTOM_CHECKER": "p360-symptom-checker", "S3_BUCKET_BATCH": "p360-batch",
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


class Rig:
    def __init__(self, db, sample):
        self.db, self.sample = db, sample
        self.blob_1, self.s3, self.wrapper = FakeBlobs(), FakeS3(), PretendWrapper()
        self.settings = settings = load_settings(ENV)
        seed.add_category(db, 1, "Kidney")
        seed.add_test(db, 900, "Extra test", name_ar="AR-Extra test", description="why", categories=(1,))
        backend = WrapperBackend(settings.backend, 5, 30, transport=httpx.MockTransport(self.wrapper))
        self.services = build_services(settings, db=db, blob=self.blob_1, s3=self.s3, backend=backend)
        self.client = TestClient(create_app(self.services))

    def post(self, document):
        path = "/api/v1/patient360/visits-symptoms" if document["source"] == 3 else "/api/v1/patient360/visits"
        return self.client.post(path, content=json.dumps(document).encode(), headers=KEY)

    def work(self):
        run_work_cycle(self.services, datetime.now(UTC))

    def aws_answers(self, source, text, **extra):
        """Play AWS and the Function app: post the answer of the last uploaded file to the app, as the Function does
        with the file it pulled from the output container of that source."""
        uploaded_source, s3_key = self.s3.uploads[-1]
        assert uploaded_source == source
        # processed/<lane>/<y>/<M>/<d>/00-input/<key>.json
        _, lane, year, month, day, _input, name = s3_key.split("/")
        key = name.removesuffix(".json")
        path = merged_output_path(lane, date(int(year), int(month), int(day)), key)
        record = {"recordId": key, "model": "model-x", "content": [{"type": "text", "text": text}],
                  "metadata": {"en": {"usage": {"input_tokens": 1, "output_tokens": 2}}}, **extra}
        return self.client.post("/api/v1/patient360/ai-results", content=json.dumps(record).encode(),
                                headers=KEY | {"X-Source": source, "X-Blob-Path": path, "Idempotency-Key": "msg-1"})


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

    assert rig.aws_answers("urgent", answer()).json() == {"outcome": "saved"}
    rig.work()
    [posted] = rig.wrapper.results
    assert (posted["patient_id"], posted["visit_id"], posted["source"]) == (PATIENT_ID, VISIT_ID, 1)
    assert posted["ai"]["note"] == both("note") and "AR-Extra test" in posted["html"]["ar"]
    assert not {"summary", "simple_summary", "critical_alerts"} & set(posted["ai"])      # only the html shows them
    assert "model-x" not in json.dumps(posted) and rig.wrapper.logins == 1

    symptom = rig.sample("symptom_check")
    assert rig.post(symptom).status_code == 202
    rig.work()
    assert [source for source, _ in rig.s3.uploads] == ["urgent", "symptom-checker"]
    assert rig.aws_answers("symptom-checker", answer(with_symptom_keys=True)).status_code == 200
    rig.work()
    assert [r["source"] for r in rig.wrapper.results] == [1, 3]
    assert rig.wrapper.results[1]["ai"]["recommendations"][0]["change_type"] == both("kept")
    assert set(rig.wrapper.results[1]) == {"success", "patient_id", "visit_id", "source", "symptom_flag", "ai", "html"}
    assert rig.wrapper.results[1]["symptom_flag"] is True and "packages" in rig.wrapper.results[1]["ai"]
    assert rig.wrapper.logins == 1                                  # one login for everything: the token is reused

    [visit_row] = seed.fetch(rig.db, "SELECT stage, tier FROM visits")
    assert visit_row["stage"] == "delivered_symptom" and visit_row["tier"] == "MILD"
    statuses = seed.fetch(rig.db, "SELECT analysis, status FROM record_processing ORDER BY analysis")
    assert [(r["analysis"], r["status"]) for r in statuses] == [("symptom", "delivered"), ("visit", "delivered")]


def test_the_function_sending_the_same_answer_again_is_a_200_duplicate_and_the_result_is_sent_once(rig):
    visit = rig.sample("urgent_mild") | {"visit_id": VISIT_ID, "patient_id": PATIENT_ID}
    assert rig.post(visit).status_code == 202
    rig.work()

    assert rig.aws_answers("urgent", answer()).json() == {"outcome": "saved"}
    assert rig.aws_answers("urgent", answer()).json() == {"outcome": "duplicate"}      # the Function retried
    rig.work()
    [posted] = rig.wrapper.results
    assert (posted["visit_id"], posted["source"]) == (VISIT_ID, 1) and posted["ai"]["note"] == both("note")


def test_the_database_down_while_saving_an_answer_is_a_500_so_the_function_retries(rig, monkeypatch):
    visit = rig.sample("urgent_mild") | {"visit_id": VISIT_ID, "patient_id": PATIENT_ID}
    assert rig.post(visit).status_code == 202
    rig.work()

    def down(*args, **kwargs):
        raise RuntimeError("database down")

    with monkeypatch.context() as patched:
        patched.setattr(rig.services.store, "find_visit", down)
        assert rig.aws_answers("urgent", answer()).status_code == 500
    assert rig.aws_answers("urgent", answer()).json() == {"outcome": "saved"}           # the retry works


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
