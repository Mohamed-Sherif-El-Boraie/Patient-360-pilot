import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from patient360 import http_app
from patient360.config.settings import load_settings
from patient360.domain.builders.backend_output import build_failed_output
from patient360.flow.dispatch import Dispatcher
from patient360.flow.intake import Intake
from patient360.flow.process import Processor
from patient360.flow.results import OutputReceiver, Received
from patient360.flow.symptom import SymptomProcessor
from patient360.http_app import create_app
from patient360.ports import BackendUnavailable
from patient360.services import Services
from tests.fakes import FakeBackend, FakeBlobs, FakeCatalogue, FakeS3, FakeStore

NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
GOOD_ENV = {
    "PGHOST": "db.example", "PGDATABASE": "patient360", "PGUSER": "app", "PGPASSWORD": "pg-password-xyz",
    "AZURE_STORAGE_ACCOUNT_URL": "https://acct.blob.core.windows.net",
    "BLOB_INPUT_CONTAINER": "ai-input", "IDEMPOTENCY_KEY_SECRET": "a-long-test-secret-value",
}
VISITS = "/api/v1/patient360/visits"
SYMPTOM_CHECKS = "/api/v1/patient360/visits-symptoms"
AI_RESULTS = "/api/v1/patient360/ai-results"
ANSWER_PATH = "normal/2026/10/24/k/05-output-merged.json"
SOURCE_HEADERS = {"X-Source": "urgent", "X-Blob-Path": ANSWER_PATH}


class Spy:
    """Records its calls; raises ``error`` if given."""

    def __init__(self, error=None, outcome="saved"):
        self.calls, self.error, self.outcome = [], error, outcome

    def run(self):
        self.calls.append(())
        if self.error:
            raise self.error

    def receive_record(self, source, path, text):
        self.calls.append((source, path, text))
        if self.error:
            raise self.error
        return [Received(None, self.outcome)]


class StubDb:
    def __init__(self, down=False):
        self.down, self.executed = down, []

    @contextmanager
    def transaction(self):
        if self.down:
            raise RuntimeError("db down")
        yield self

    def execute(self, sql):
        self.executed.append(sql)


class Rig:
    def __init__(self, sample, db=None, **env):
        self.sample, self.store, self.blobs = sample, FakeStore(), FakeBlobs()
        visit = sample("urgent_mild")
        categories = {p["parameter_id"]: [1] for t in visit["tests"] for p in t["results"]}
        catalogue = FakeCatalogue(categories)
        self.settings = load_settings({**GOOD_ENV, **env})
        self.spy_receiver = Spy()
        self.batch_spy = Spy()
        self.services = Services(
            settings=self.settings, db=db or StubDb(), store=self.store,
            intake=Intake(self.store, self.blobs, b"a-long-test-secret-value", lambda: NOW,
                          self.settings.max_request_bytes),
            processor=Processor(self.store, catalogue, self.blobs),
            symptom_processor=SymptomProcessor(self.store, self.blobs),
            batch_trigger=self.batch_spy,
            dispatcher=Dispatcher(self.store, self.blobs, FakeS3(), lambda: NOW),
            receiver=self.spy_receiver, sender=None)
        self.client = TestClient(create_app(self.services))

    def post(self, document, path=None, **kwargs):
        if path is None:       # the symptom check has its own endpoint (it comes from the wrapper)
            path = SYMPTOM_CHECKS if isinstance(document, dict) and document.get("source") == 3 else VISITS
        body = document if isinstance(document, bytes) else json.dumps(document).encode()
        return self.client.post(path, content=body, **kwargs)


@pytest.fixture
def rig(sample):
    return Rig(sample)


def test_a_valid_urgent_visit_is_accepted_and_then_processed(rig, sample):
    visit = sample("urgent_mild")
    response = rig.post(visit)
    assert response.status_code == 202
    assert response.json() == {"response": {"code": -1, "message": "Accepted"}, "data": {"visit_id": visit["visit_id"]}}
    assert rig.store.visits[visit["visit_id"]]["stage"] == "processed"
    assert rig.batch_spy.calls == []


def test_a_visit_full_of_blank_values_is_accepted_and_processed(rig, sample):
    visit = sample("urgent_mild")
    for result in (r for test in visit["tests"] for r in test["results"]):
        result.update(lab_flag="", unit=" ", parameter_remark="", critical={"low": "", "high": "", "range_text": ""})
        result["reference"].update(low="", high="", range="")
    visit["tests"][0]["results"][0]["value"] = ""
    visit["tests"][0]["test_remark"] = "  "
    assert rig.post(visit).status_code == 202
    assert rig.store.visits[visit["visit_id"]]["stage"] == "processed"


def test_a_batch_visit_also_runs_the_batch_trigger(rig, sample):
    visit = sample("batch_normal")
    assert rig.post(visit).status_code == 202
    assert rig.store.visits[visit["visit_id"]]["stage"] == "processed"
    assert rig.batch_spy.calls == [()]


def test_a_symptom_check_for_a_stored_visit_is_accepted_and_processed(rig, sample):
    visit = sample("urgent_mild")
    symptom = sample("symptom_check")
    visit.update(visit_id=symptom["visit_id"], patient_id=symptom["patient_id"])
    assert rig.post(visit).status_code == 202
    assert rig.post(symptom).status_code == 202
    # no visit result yet, so the processor leaves it waiting: the call happened and did not crash
    assert rig.store.symptom_checks[symptom["visit_id"]]["stage"] == "received_symptom"


def test_a_symptom_check_calls_the_symptom_processor(rig, sample):
    calls = []
    symptom_processor = rig.services.symptom_processor
    symptom_processor.process = lambda visit_id: calls.append(visit_id)
    visit, symptom = sample("urgent_mild"), sample("symptom_check")
    visit.update(visit_id=symptom["visit_id"], patient_id=symptom["patient_id"])
    rig.post(visit)
    rig.post(symptom)
    assert calls == [symptom["visit_id"]]


def test_each_kind_of_payload_is_only_accepted_at_its_own_endpoint(rig, sample):
    wrong_for_visits = rig.post(sample("symptom_check"), VISITS)
    wrong_for_symptoms = rig.post(sample("urgent_mild"), SYMPTOM_CHECKS)
    for reply in (wrong_for_visits, wrong_for_symptoms):
        assert reply.status_code == 422 and reply.json()["errors"][0]["type"] == "wrong_endpoint"
    assert rig.post(sample("urgent_mild"), VISITS).status_code == 202


def test_a_source_sent_as_text_is_accepted(rig, sample):
    visit = sample("urgent_mild") | {"source": "1"}
    assert rig.post(visit).status_code == 202


def test_a_duplicate_is_answered_200(rig, sample):
    visit = sample("urgent_mild")
    rig.post(visit)
    response = rig.post(visit)
    assert response.status_code == 200
    assert response.json()["response"] == {"code": -1, "message": "Duplicate"}


def test_invalid_json_and_a_wrong_field_are_422_with_errors_and_no_echo(rig, sample):
    response = rig.post(b"{not json at all SECRET-PATIENT")
    assert response.status_code == 422 and response.json()["errors"]
    assert "SECRET-PATIENT" not in response.text
    visit = sample("urgent_mild")
    visit["patient_id"] = "SECRET-PATIENT-ID"
    response = rig.post(visit)
    body = response.json()
    assert response.status_code == 422 and body["response"] == {"code": 1, "message": "Invalid payload"}
    assert body["errors"] and "SECRET-PATIENT-ID" not in response.text


def test_a_symptom_check_for_a_visit_with_a_closed_checker_is_409_and_nothing_is_processed(rig, sample):
    visit, symptom = sample("urgent_mild"), sample("symptom_check")
    visit.update(visit_id=symptom["visit_id"], patient_id=symptom["patient_id"])
    assert rig.post(visit).status_code == 202
    rig.store.visits[visit["visit_id"]]["public_output"] = {"symptom_flag": False}
    response = rig.post(symptom)
    assert response.status_code == 409
    assert response.json() == {"response": {"code": 1, "message": "SYMPTOM_CHECKER_CLOSED"}}
    assert not rig.store.symptom_checks


def test_a_symptom_check_for_an_unknown_visit_is_404(rig, sample):
    response = rig.post(sample("symptom_check"))
    assert response.status_code == 404
    assert response.json() == {"response": {"code": 1, "message": "VISIT_NOT_FOUND"}}


def test_a_body_over_the_limit_is_413_by_content_length_without_reading_it(sample):
    rig = Rig(sample, MAX_REQUEST_BYTES="100")
    response = rig.post(sample("urgent_mild"))
    assert response.status_code == 413
    assert response.json() == {"response": {"code": 1, "message": "Payload too large"}}
    assert not rig.store.visits and not rig.store.rejected


def test_too_large_from_intake_is_413_when_there_is_no_content_length(sample):
    rig = Rig(sample, MAX_REQUEST_BYTES="100")
    response = rig.client.post(VISITS, content=iter([json.dumps(sample("urgent_mild")).encode()]))   # chunked
    assert response.status_code == 413 and not rig.store.visits


@pytest.mark.parametrize("headers, status", [({}, 401), ({"X-Api-Key": "wrong"}, 401), ({"X-Api-Key": "s3cret"}, 202)])
def test_the_api_key(sample, headers, status):
    rig = Rig(sample, INBOUND_API_KEY="s3cret")
    response = rig.post(sample("urgent_mild"), headers=headers)
    assert response.status_code == status
    if status == 401:
        assert response.json() == {"response": {"code": 1, "message": "Unauthorized"}}
        assert not rig.store.visits
    assert "s3cret" not in response.text


@pytest.mark.parametrize("headers, status", [({}, 401), ({"X-Api-Key": "wrong"}, 401), ({"X-Api-Key": "s3cret"}, 200)])
def test_the_api_key_also_guards_the_ai_results_route_but_not_health(sample, headers, status):
    rig = Rig(sample, INBOUND_API_KEY="s3cret")
    response = rig.client.post(AI_RESULTS, content=b"{}", headers=SOURCE_HEADERS | headers)
    assert response.status_code == status
    assert (rig.spy_receiver.calls == []) == (status == 401)
    assert rig.client.get("/health").status_code == 200


def test_without_an_api_key_setting_nothing_is_checked(rig, sample):
    assert rig.post(sample("urgent_mild"), headers={"X-Api-Key": "anything"}).status_code == 202


def test_a_database_failure_in_intake_is_500_never_202(rig, sample):
    rig.store.fail_on_save = True
    response = rig.post(sample("urgent_mild"))
    assert response.status_code == 500
    assert response.json() == {"response": {"code": 1, "message": "Internal error"}}


def test_a_background_failure_does_not_break_the_response(rig, sample):
    rig.services.processor.process = Spy(RuntimeError("boom")).run
    response = rig.post(sample("urgent_mild"))
    assert response.status_code == 202


def test_an_ai_result_goes_to_the_receiver_with_the_source_path_and_body_and_answers_its_outcome(rig):
    response = rig.client.post(AI_RESULTS, content=b'{"recordId": "k"}', headers=SOURCE_HEADERS | {
        "Idempotency-Key": "abc"})
    assert response.status_code == 200 and response.json() == {"outcome": "saved"}
    assert rig.spy_receiver.calls == [("urgent", ANSWER_PATH, '{"recordId": "k"}')]


@pytest.mark.parametrize("headers", [{}, {"X-Source": "urgent"}, {"X-Blob-Path": ANSWER_PATH},
                                     {"X-Source": "weekly", "X-Blob-Path": ANSWER_PATH},
                                     {"X-Source": "urgent", "X-Blob-Path": ""}])
def test_an_ai_result_with_a_missing_or_unknown_source_or_path_is_400(rig, headers):
    response = rig.client.post(AI_RESULTS, content=b"{}", headers=headers)
    assert response.status_code == 400 and rig.spy_receiver.calls == []


def test_an_oversized_ai_result_is_413_before_it_is_read_and_also_when_chunked(rig):
    big = b"x" * (http_app.MAX_RESULT_BYTES + 1)
    assert rig.client.post(AI_RESULTS, content=big, headers=SOURCE_HEADERS).status_code == 413
    assert rig.client.post(AI_RESULTS, content=iter([big]), headers=SOURCE_HEADERS).status_code == 413
    assert rig.spy_receiver.calls == []


def test_a_receiver_failure_answers_500_so_the_function_retries_and_logs_only_the_error_type(rig):
    rig.spy_receiver.error = RuntimeError("boom with PATIENT-DATA")
    with capture_logs() as logs:
        response = rig.client.post(AI_RESULTS, content=b"{}", headers=SOURCE_HEADERS)
    assert response.status_code == 500 and response.json() == {"response": {"code": 1, "message": "Internal error"}}
    assert {"event": "request_failed", "error_type": "RuntimeError", "log_level": "error"} in logs
    assert "PATIENT-DATA" not in str(logs)


@pytest.mark.parametrize("source, path", [
    ("batch", "normal/2026/10/25/" + "a" * 32 + "/05-output-merged.json"),      # a batch visit copy
    ("urgent", "normal/2026/10/24/" + "a" * 32 + "/04-output-en.json"),         # another file of AWS
    ("urgent", "somewhere/else.json"),
])
def test_a_path_that_is_not_an_answer_is_answered_200_ignored(sample, source, path):
    rig = Rig(sample)
    rig.services = replace(rig.services, receiver=OutputReceiver(rig.store, FakeCatalogue()))
    client = TestClient(create_app(rig.services))
    response = client.post(AI_RESULTS, content=b"{}", headers={"X-Source": source, "X-Blob-Path": path})
    assert response.status_code == 200 and response.json() == {"outcome": "ignored"}


def test_a_visit_answer_is_saved_and_an_unknown_visit_is_200(sample):
    rig = Rig(sample)
    rig.services = replace(rig.services, receiver=OutputReceiver(rig.store, FakeCatalogue()))
    client = TestClient(create_app(rig.services))
    unknown = json.dumps({"recordId": "a" * 32}).encode()
    response = client.post(AI_RESULTS, content=unknown, headers=SOURCE_HEADERS)
    assert response.status_code == 200 and response.json() == {"outcome": "unknown_visit"}
    assert client.post(AI_RESULTS, content=b"not json", headers=SOURCE_HEADERS).json() == {"outcome": "invalid_output"}


def test_a_failed_file_is_answered_200_failed_recorded(sample):
    rig = Rig(sample)
    visit = sample("urgent_mild")
    rig.post(visit)
    key = rig.store.visits[visit["visit_id"]]["idempotency_key"]
    rig.services = replace(rig.services, receiver=OutputReceiver(rig.store, FakeCatalogue()))
    client = TestClient(create_app(rig.services))
    headers = {"X-Source": "urgent", "X-Blob-Path": f"normal/2026/10/24/{key}/07-failed.json"}
    response = client.post(AI_RESULTS, content=b"", headers=headers)
    assert response.status_code == 200 and response.json() == {"outcome": "failed_recorded"}
    assert rig.store.visits[visit["visit_id"]]["stage"] == "failed"


def rejected_rig(sample, backend):
    rig = Rig(sample)
    rig.services = replace(rig.services, backend=backend)
    rig.client = TestClient(create_app(rig.services))
    visit = sample("urgent_mild")
    visit["status"] = "high"            # fails validation, ids stay usable
    return rig, visit


def test_a_rejected_payload_with_usable_ids_tells_the_backend_the_patient_failed_once(sample):
    backend = FakeBackend()
    rig, visit = rejected_rig(sample, backend)
    response = rig.post(visit)
    assert response.status_code == 422 and response.json()["response"] == {"code": 1, "message": "Invalid payload"}
    assert backend.posts == [build_failed_output(patient_id=visit["patient_id"], visit_id=visit["visit_id"], source=1)]


def test_a_rejected_payload_without_usable_ids_or_without_a_backend_sends_nothing(sample):
    backend = FakeBackend()
    rig, visit = rejected_rig(sample, backend)
    assert rig.post(visit | {"patient_id": "x"}).status_code == 422
    assert rig.post(b"{not json").status_code == 422
    assert backend.posts == []
    rig_without, visit = rejected_rig(sample, None)
    assert rig_without.post(visit).status_code == 422


def test_a_backend_error_while_telling_about_a_rejected_payload_is_swallowed_and_logged(sample):
    backend = FakeBackend()
    backend.raises = BackendUnavailable
    rig, visit = rejected_rig(sample, backend)
    with capture_logs() as logs:
        response = rig.post(visit)
    assert response.status_code == 422
    assert {"event": "rejected_failed_message_not_sent", "error_type": "BackendUnavailable",
            "log_level": "warning"} in logs


def test_health_ok_and_database_unavailable(sample):
    ok = Rig(sample)
    assert ok.client.get("/health").json() == {"status": "ok"}
    assert ok.services.db.executed == ["SELECT 1"]
    down = Rig(sample, db=StubDb(down=True))
    response = down.client.get("/health")
    assert response.status_code == 503 and response.json() == {"status": "database_unavailable"}
