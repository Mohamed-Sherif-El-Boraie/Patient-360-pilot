import json
from datetime import UTC, datetime

import pytest

from patient360.domain.common.ids import make_idempotency_key
from patient360.failure import StepFailed
from patient360.flow.intake import (
    Accepted,
    Duplicate,
    Intake,
    Parked,
    Rejected,
    Retry,
    SymptomAgain,
    SymptomCheckerClosed,
    TooLarge,
    VisitNotFound,
)
from patient360.flow.process import Processor
from tests.fakes import FakeBlobs, FakeCatalogue, FakeStore

SECRET = b"a-long-test-secret-value"
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)


@pytest.fixture
def parts():
    store, blobs = FakeStore(), FakeBlobs()
    return Intake(store, blobs, SECRET, lambda: NOW), store, blobs


def raw(payload) -> bytes:
    return json.dumps(payload).encode()


def key_of(payload) -> str:
    return make_idempotency_key(SECRET, patient_id=payload["patient_id"], visit_id=payload["visit_id"],
                                company_id=payload["company_id"])


def test_a_valid_visit_is_saved_raw_in_blob_and_in_the_database_and_accepted(parts, sample):
    intake, store, blobs = parts
    visit = sample("urgent_mild")
    body = raw(visit)
    outcome = intake.receive(body)
    key = key_of(visit)
    assert outcome == Accepted(visit["visit_id"], key, visit["source"])
    assert blobs.files[f"raw-jsons/urgent/2026/10/24/{key}.json"] == body
    saved = store.visits[visit["visit_id"]]
    assert saved["raw"] == body and saved["idempotency_key"] == key and saved["read_from"] == "blob"
    assert saved["received_at"] == NOW and saved["payload"]["tests"][0]["test_id"] == visit["tests"][0]["test_id"]


def test_the_same_visit_again_is_a_retry_until_its_result_is_delivered_and_a_duplicate_after_and_changes_nothing(
        parts, sample):
    intake, store, blobs = parts
    visit = sample("urgent_mild")
    body = raw(visit)
    intake.receive(body)
    files_before = dict(blobs.files)
    assert intake.receive(body) == Retry(visit["visit_id"])           # asked again before the result was delivered
    store.visits[visit["visit_id"]]["stage"] = "delivered"
    assert intake.receive(body) == Duplicate(visit["visit_id"])
    assert blobs.files == files_before and len(store.visits) == 1


def test_a_retry_puts_a_result_whose_delivery_gave_up_back_on_the_queue(parts, sample):
    intake, store, _ = parts
    visit = sample("urgent_mild")
    body = raw(visit)
    intake.receive(body)
    record = store.visits[visit["visit_id"]]
    record.update(stage="failed", delivered=True, delivery_attempts=5)        # the five tries were used up
    assert intake.receive(body) == Retry(visit["visit_id"])
    assert (record["stage"], record["delivered"], record["delivery_attempts"]) == ("post_processing", False, 0)


def test_a_batch_visit_goes_to_the_batch_raw_folder(parts, sample):
    intake, _, blobs = parts
    visit = sample("batch_normal")
    intake.receive(raw(visit))
    assert f"raw-jsons/batch/2026/10/24/{key_of(visit)}.json" in blobs.files


def test_an_invalid_payload_is_rejected_saved_in_blob_and_database_and_never_becomes_a_visit(parts, sample):
    intake, store, blobs = parts
    visit = sample("urgent_mild")
    visit["status"] = "high"
    body = raw(visit)
    outcome = intake.receive(body)
    assert isinstance(outcome, Rejected) and outcome.errors[0]["loc"] == ["status"]
    path = f"_rejected/urgent/2026/10/24/{visit['visit_id']}.json"
    assert blobs.files[path] == body
    assert store.rejected == [{"source_id": 1, "raw": body, "errors": outcome.errors, "blob_path": path}]
    assert store.visits == {}
    assert (outcome.patient_id, outcome.visit_id, outcome.source_id) == (visit["patient_id"], visit["visit_id"], 1)


def test_a_rejection_without_a_usable_patient_id_carries_none_for_it(parts, sample):
    intake, _, _ = parts
    visit = sample("urgent_mild") | {"patient_id": "not a number"}
    outcome = intake.receive(raw(visit))
    assert isinstance(outcome, Rejected) and outcome.patient_id is None and outcome.visit_id == visit["visit_id"]


def test_a_payload_that_is_not_json_is_rejected_under_unknown(parts):
    intake, store, blobs = parts
    outcome = intake.receive(b"{not json")
    assert isinstance(outcome, Rejected) and outcome.errors[0]["type"] == "json_invalid"
    (path,) = blobs.files
    assert path.startswith("_rejected/unknown/2026/10/24/unknown_") and store.rejected[0]["source_id"] is None


def test_an_unknown_source_or_a_bad_visit_id_is_rejected_under_unknown(parts, sample):
    intake, store, blobs = parts
    visit = sample("urgent_mild")
    visit["source"] = 9
    intake.receive(raw(visit))
    visit["source"], visit["visit_id"] = 1, -4
    intake.receive(raw(visit))
    assert len(store.rejected) == 2 and all("unknown" in path for path in blobs.files)


def test_the_rejection_error_text_never_holds_the_patient_data(parts, sample):
    intake, store, _ = parts
    visit = sample("urgent_mild")
    visit["patient_id"] = "CANARY-PATIENT"
    outcome = intake.receive(raw(visit))
    assert "CANARY-PATIENT" not in json.dumps(outcome.errors)


def test_when_blob_is_down_the_visit_is_still_accepted_and_marked_for_postgresql(parts, sample):
    intake, store, blobs = parts
    blobs.down = True
    visit = sample("urgent_mild")
    body = raw(visit)
    assert isinstance(intake.receive(body), Accepted)
    saved = store.visits[visit["visit_id"]]
    assert saved["read_from"] == "postgresql" and saved["raw"] == body
    assert blobs.files == {}


def test_when_blob_is_down_a_rejection_is_still_saved_in_the_database_without_a_blob_path(parts, sample):
    intake, store, blobs = parts
    blobs.down = True
    visit = sample("urgent_mild")
    visit["status"] = "high"
    assert isinstance(intake.receive(raw(visit)), Rejected)
    assert store.rejected[0]["blob_path"] is None and store.rejected[0]["raw"] == raw(visit)


def test_a_file_that_is_already_in_blob_does_not_stop_the_visit(parts, sample):
    intake, store, blobs = parts
    visit = sample("urgent_mild")
    blobs.files[f"raw-jsons/urgent/2026/10/24/{key_of(visit)}.json"] = b"an earlier attempt"
    assert isinstance(intake.receive(raw(visit)), Accepted)
    assert store.visits[visit["visit_id"]]["read_from"] == "blob"


def test_if_the_database_fails_the_payload_is_parked_in_blob_and_the_sender_is_told_it_is_accepted(parts, sample):
    intake, store, blobs = parts
    store.fail_on_save = True
    visit = sample("urgent_mild")
    assert intake.receive(raw(visit)) == Parked(visit["visit_id"])
    assert store.visits == {}
    [path] = [name for name in blobs.files if name.startswith("_pending/")]
    assert blobs.files[path] == raw(visit)


def test_if_the_database_and_blob_both_fail_nothing_is_safe_and_the_failure_is_raised(parts, sample):
    intake, store, blobs = parts
    store.fail_on_save, blobs.down = True, True
    with pytest.raises(StepFailed) as failed:
        intake.receive(raw(sample("urgent_mild")))
    assert failed.value.step == "saving the visit in the database" and isinstance(failed.value.error, RuntimeError)
    assert store.visits == {}


def test_a_rejected_payload_is_answered_even_if_its_copy_cannot_be_saved(parts, sample):
    intake, store, _ = parts
    store.save_rejected = lambda *args: (_ for _ in ()).throw(RuntimeError("database down"))
    bad = sample("urgent_mild")
    bad["patient_id"] = "x"
    assert isinstance(intake.receive(raw(bad)), Rejected)


def symptom_for(visit, sample):
    body = sample("symptom_check")
    body["patient_id"], body["visit_id"] = visit["patient_id"], visit["visit_id"]
    return body


def test_a_symptom_check_is_refused_when_the_visit_result_says_the_checker_is_closed(parts, sample):
    intake, store, blobs = parts
    visit = sample("urgent_mild")
    intake.receive(raw(visit))
    store.visits[visit["visit_id"]]["public_output"] = {"symptom_flag": False}      # a fully normal or blocked result
    files_before = dict(blobs.files)
    assert intake.receive(raw(symptom_for(visit, sample))) == SymptomCheckerClosed(visit["visit_id"])
    assert not store.symptom_checks and blobs.files == files_before                 # nothing was saved


def test_a_symptom_check_is_accepted_when_the_flag_is_true_or_the_visit_has_no_result_yet(parts, sample):
    intake, store, _ = parts
    visit = sample("urgent_mild")
    intake.receive(raw(visit))
    assert isinstance(intake.receive(raw(symptom_for(visit, sample))), Accepted)      # no result yet: it waits
    store.symptom_checks.clear()
    store.visits[visit["visit_id"]]["public_output"] = {"symptom_flag": True}
    assert isinstance(intake.receive(raw(symptom_for(visit, sample))), Accepted)
    store.symptom_checks.clear()
    store.visits[visit["visit_id"]]["public_output"] = {"ai": {}}                      # an old result without the flag
    assert isinstance(intake.receive(raw(symptom_for(visit, sample))), Accepted)


def test_a_symptom_check_started_again_is_a_success_that_says_the_checker_is_closed(parts, sample):
    intake, store, _ = parts
    visit = sample("urgent_mild")
    intake.receive(raw(visit))
    intake.receive(raw(symptom_for(visit, sample)))
    store.visits[visit["visit_id"]]["public_output"] = {"symptom_flag": False}
    expected = SymptomAgain({"success": True, "patient_id": visit["patient_id"], "visit_id": visit["visit_id"],
                             "company_id": visit["company_id"], "source": 3, "symptom_flag": False})
    assert intake.receive(raw(symptom_for(visit, sample))) == expected         # not delivered yet
    store.symptom_checks[visit["visit_id"]]["stage"] = "delivered_symptom"
    assert intake.receive(raw(symptom_for(visit, sample))) == expected         # delivered: the same answer, no error


def test_a_symptom_check_for_a_stored_visit_is_accepted_once(parts, sample):
    intake, store, blobs = parts
    visit = sample("urgent_mild")
    intake.receive(raw(visit))
    symptom = symptom_for(visit, sample)
    body = raw(symptom)
    assert intake.receive(body) == Accepted(visit["visit_id"], key_of(visit), 3)      # source 3: the symptom checker
    assert blobs.files[f"raw-jsons/symptom-checker/2026/10/24/{key_of(visit)}.json"] == body
    assert store.symptom_checks[visit["visit_id"]]["read_from"] == "blob"
    assert isinstance(intake.receive(body), SymptomAgain)


def test_a_symptom_check_for_an_unknown_visit_or_another_patient_is_not_found_and_saves_nothing(parts, sample):
    intake, store, blobs = parts
    visit = sample("urgent_mild")
    symptom = symptom_for(visit, sample)
    assert intake.receive(raw(symptom)) == VisitNotFound(visit["visit_id"])
    intake.receive(raw(visit))
    files_before = dict(blobs.files)
    symptom["patient_id"] = visit["patient_id"] + 1
    assert intake.receive(raw(symptom)) == VisitNotFound(visit["visit_id"])
    assert store.symptom_checks == {} and blobs.files == files_before


def test_a_symptom_check_with_blob_down_is_kept_in_postgresql(parts, sample):
    intake, store, blobs = parts
    visit = sample("urgent_mild")
    intake.receive(raw(visit))
    blobs.down = True
    assert isinstance(intake.receive(raw(symptom_for(visit, sample))), Accepted)
    assert store.symptom_checks[visit["visit_id"]]["read_from"] == "postgresql"


def test_an_invalid_symptom_check_is_rejected(parts, sample):
    intake, store, _ = parts
    symptom = sample("symptom_check")
    symptom["patient_symptoms"] = "   "
    assert isinstance(intake.receive(raw(symptom)), Rejected) and len(store.rejected) == 1


def test_no_size_limit_by_default_and_a_set_limit_refuses_a_bigger_body_without_saving_it(sample):
    visit = sample("urgent_mild")
    body = raw(visit)
    store, blobs = FakeStore(), FakeBlobs()
    assert isinstance(Intake(store, blobs, SECRET, lambda: NOW).receive(body), Accepted)

    store, blobs = FakeStore(), FakeBlobs()
    limited = Intake(store, blobs, SECRET, lambda: NOW, max_bytes=len(body) - 1)
    assert limited.receive(body) == TooLarge(len(body), len(body) - 1)
    assert not store.visits and not store.rejected and not blobs.files

    exact = Intake(store, blobs, SECRET, lambda: NOW, max_bytes=len(body))
    assert isinstance(exact.receive(body), Accepted)


def test_a_visit_is_saved_already_processed_in_one_database_write(sample):
    store, blobs = FakeStore(), FakeBlobs()
    intake = Intake(store, blobs, SECRET, lambda: NOW, prepare=Processor(store, FakeCatalogue(), blobs).prepare)
    visit = sample("urgent_mild")
    outcome = intake.receive(raw(visit))
    assert outcome == Accepted(visit["visit_id"], key_of(visit), visit["source"], processed=True)
    saved = store.visits[visit["visit_id"]]
    assert saved["stage"] == "processed" and saved["tier"] and saved["model_input"]
    assert any(name.startswith("processed/") for name in blobs.files)      # the file AWS reads is in Blob too


def test_a_visit_whose_processing_failed_is_still_accepted_and_left_for_the_background(sample):
    store, blobs = FakeStore(), FakeBlobs()

    def broken(*_):
        raise RuntimeError("catalogue down")
    outcome = Intake(store, blobs, SECRET, lambda: NOW, prepare=broken).receive(raw(sample("urgent_mild")))
    assert isinstance(outcome, Accepted) and outcome.processed is False
    assert store.visits[outcome.visit_id]["stage"] == "received"
