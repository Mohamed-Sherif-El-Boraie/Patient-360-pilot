"""The receiving side of the PostgreSQL store, against a real database."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from patient360.adapters.postgres.db import Database, sha256_hex
from patient360.adapters.postgres.visits import VisitsMixin
from tests.adapters.postgres import seed

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


class Store(VisitsMixin):
    def __init__(self, db: Database) -> None:
        self._db = db


@pytest.fixture
def store(db):
    return Store(db)


def _save(store, payload, *, raw=None, key=None, at=NOW):
    raw = raw if raw is not None else json.dumps(payload).encode()
    store.save_visit(payload, raw, key or f"key-{payload['visit_id']}", at, "blob")
    return raw


def _count(db, table):
    return seed.fetch(db, f"SELECT count(*) AS n FROM {table}")[0]["n"]


def test_an_age_sent_as_text_is_stored_as_a_number_of_days(db, store, sample):
    payload = sample("urgent_mild")
    payload["patient"]["age_days"] = "19200"
    _save(store, payload)
    assert seed.fetch(db, "SELECT age_days FROM visits")[0]["age_days"] == 19200


def test_save_visit_urgent_mild(db, store, sample):
    payload = sample("urgent_mild")
    raw = _save(store, payload)
    vid = payload["visit_id"]

    [visit] = seed.fetch(db, "SELECT * FROM visits WHERE visit_id = %s", (vid,))
    assert (visit["patient_id"], visit["idempotency_key"], visit["source_id"]) == (
        payload["patient_id"], f"key-{vid}", 1)
    assert (visit["company_id"], visit["sex_id"], visit["stage"], visit["created_at"]) == (10, 1, "received", NOW)
    assert visit["age_days"] > 365 * 50

    assert [r["test_id"] for r in seed.fetch(db, "SELECT test_id FROM visit_tests ORDER BY test_id")] == [18, 2241]
    rows = {r["parameter_id"]: r for r in seed.fetch(db, "SELECT * FROM lab_results")}
    assert rows[2]["value"] == "95"
    assert (rows[2]["status"], rows[2]["alert"], rows[2]["lab_flag"]) == ("normal", "NORMAL", None)
    assert rows[2234]["value"] == "168"
    assert (rows[2234]["status"], rows[2234]["alert"], rows[2234]["lab_flag"]) == ("high", "MEDICAL_ATTENTION", "High")
    assert rows[2234]["reference_range"].startswith("Normal: 70 - 139\r\n")
    assert rows[2234]["parameter_remark"] == "Fructosamine and HBA1c are recommended."
    assert rows[2234]["critical_low"] is None

    [history] = seed.fetch(db, "SELECT * FROM lab_result_history")
    assert (history["parameter_id"], history["history_visit_id"], history["history_value"]) == (2234, 2069999903, "149")
    assert [(a["preparation_id"], a["answer"]) for a in seed.fetch(db, "SELECT * FROM visit_answers ORDER BY 2")] == [
        (900, "10"), (901, "No")]
    assert _count(db, "descriptive_results") == 0

    [stored] = seed.fetch(db, "SELECT raw_bytes, sha256 FROM record_payloads WHERE visit_id = %s AND kind = 'raw'",
                          (vid,))
    assert bytes(stored["raw_bytes"]) == raw
    assert stored["sha256"] == sha256_hex(raw)


def test_save_visit_critical_values_and_exact_text(db, store, sample):
    payload = sample("on_demand_critical")
    raw = json.dumps(payload).replace('"value": "5.3"', '"value": 5.30').encode()   # a number, trailing zero
    _save(store, payload, raw=raw)

    rows = seed.fetch(db, "SELECT * FROM lab_results")
    assert any(r["value"] == "5.30" for r in rows)
    assert {r["alert"] for r in rows} >= {"CRITICAL"}
    critical = [r for r in rows if r["status"] == "critical"]
    assert critical and all(r["critical_direction"] in ("critical_low", "critical_high") for r in critical)
    assert {h["history_value"] for h in seed.fetch(db, "SELECT history_value FROM lab_result_history")} == {
        "241", "205.5"}
    assert seed.fetch(db, "SELECT raw_bytes FROM record_payloads")[0]["raw_bytes"] == raw


def test_descriptive_results_use_plain_text_and_history(db, store, sample):
    payload = sample("descriptive_only")
    payload["descriptive_results"][0]["text"] = "<p>No <b>deletions</b> found</p>"
    payload["descriptive_results"][0]["history"] = [
        {"history_visit_id": 7, "history_date": "2026-01-02", "history_value": "<p>Old <i>text</i><br>here</p>"}]
    _save(store, payload)

    [result] = seed.fetch(db, "SELECT * FROM descriptive_results")
    assert (result["test_id"], result["status"]) == (2380, "text")
    assert "<" not in result["text_value"] and "deletions" in result["text_value"]
    [history] = seed.fetch(db, "SELECT * FROM descriptive_result_history")      # the history is plain text too
    assert (history["history_visit_id"], history["history_value"]) == (7, "Old text\nhere")


def test_history_rows_carry_the_company_of_their_visit(db, store, sample):
    payload = sample("descriptive_only")
    payload["company_id"] = 77                      # unknown company: added first, then the history points to it
    payload["descriptive_results"][0]["history"] = [
        {"history_visit_id": 7, "history_date": "2026-01-02", "history_value": "old"}]
    _save(store, payload)
    assert {h["company_id"] for h in seed.fetch(db, "SELECT company_id FROM descriptive_result_history")} == {77}

    lab = sample("urgent_mild")
    lab["company_id"] = 15
    _save(store, lab)
    assert {h["company_id"] for h in seed.fetch(db, "SELECT company_id FROM lab_result_history")} == {15}


def test_unknown_company_test_and_parameter_get_placeholders(db, store, sample):
    payload = sample("urgent_mild")
    payload["company_id"] = 77
    _save(store, payload)

    assert seed.fetch(db, "SELECT * FROM companies WHERE company_id = 77") == [
        {"company_id": 77, "company_name": "Company 77"}]
    test = seed.fetch(db, "SELECT test_name_en, active FROM tests WHERE test_id = 18")
    assert test == [{"test_name_en": "Fasting Blood Glucose", "active": False}]
    assert seed.fetch(db, "SELECT parameter_name, active FROM parameters WHERE parameter_id = 2") == [
        {"parameter_name": "Fasting Blood Glucose", "active": False}]


def test_known_company_test_and_parameter_are_left_alone(db, store, sample):
    seed.add_test(db, 18, "Glucose (reviewed)", categories=(1,))
    seed.add_parameter(db, 2, "Glucose parameter")
    _save(store, sample("urgent_mild"))

    assert seed.fetch(db, "SELECT test_name_en, active FROM tests WHERE test_id = 18") == [
        {"test_name_en": "Glucose (reviewed)", "active": True}]
    assert seed.fetch(db, "SELECT parameter_name FROM parameters WHERE parameter_id = 2") == [
        {"parameter_name": "Glucose parameter"}]
    assert seed.fetch(db, "SELECT company_name FROM companies WHERE company_id = 10") == [
        {"company_name": "Al-Mokhtbar"}]


def test_a_failure_halfway_leaves_nothing(db, store, sample):
    payload = sample("urgent_mild")
    payload["company_id"] = 77
    payload["visit_answer_list"][1]["preparation_id"] = payload["visit_answer_list"][0]["preparation_id"]
    with pytest.raises(psycopg.errors.UniqueViolation):
        _save(store, payload)

    for table in ("visits", "visit_tests", "lab_results", "lab_result_history", "visit_answers", "record_payloads"):
        assert _count(db, table) == 0
    assert _count(db, "tests") == 0 and _count(db, "parameters") == 0
    assert seed.fetch(db, "SELECT 1 FROM companies WHERE company_id = 77") == []


def test_duplicate_visit_is_refused(store, sample):
    _save(store, sample("urgent_mild"))
    with pytest.raises(psycopg.errors.UniqueViolation):
        _save(store, sample("urgent_mild"), key="another-key")


def test_visit_exists_stored_visit_and_find_visit(store, sample):
    payload = sample("urgent_mild")
    assert not store.visit_exists(payload["visit_id"])
    raw = _save(store, payload, key="the-key")

    assert store.visit_exists(payload["visit_id"])
    stored = store.stored_visit(payload["visit_id"])
    assert (stored.visit_id, stored.idempotency_key, stored.source_id) == (payload["visit_id"], "the-key", 1)
    assert (stored.received_at, stored.raw) == (NOW, raw)
    assert store.find_visit("the-key") == stored
    assert store.find_visit("nope") is None
    with pytest.raises(LookupError):
        store.stored_visit(1)


def test_visits_waiting_for_processing(db, store):
    for visit_id, age in ((3, 30), (1, 30), (2, 40), (4, 5), (5, 50)):
        seed.add_visit(db, visit_id, created_at=NOW - timedelta(minutes=age))
    seed.add_processing(db, 5)                                    # processed: excluded
    seed.add_processing(db, 3, analysis="symptom")                # only its symptom was processed: still waits

    assert store.visits_waiting_for_processing(NOW - timedelta(minutes=10)) == [2, 1, 3]


def test_symptom_check_states(db, store):
    seed.add_visit(db, 10, patient_id=500)
    assert store.symptom_check_state(99, 500) == "no_visit"
    assert store.symptom_check_state(10, 501) == "no_visit"
    assert store.symptom_check_state(10, 500) == "ok"

    store.save_symptom_check({"visit_id": 10, "patient_symptoms": "cough"}, b'{"visit_id": 10}', NOW, "blob")
    assert store.symptom_check_state(10, 500) == "already_used"


def test_a_closed_symptom_checker_is_read_from_the_visit_symptom_flag_column(db, store):
    seed.add_visit(db, 10, patient_id=500)
    assert store.symptom_check_state(10, 500) == "ok"                      # no result yet (NULL): still open
    seed.fetch(db, "UPDATE visits SET symptom_flag = true WHERE visit_id = 10 RETURNING 1")
    assert store.symptom_check_state(10, 500) == "ok"
    seed.fetch(db, "UPDATE visits SET symptom_flag = false WHERE visit_id = 10 RETURNING 1")
    assert store.symptom_check_state(10, 500) == "closed"
    assert store.symptom_check_state(10, 501) == "no_visit"                # another patient: not closed, unknown


def test_save_and_read_symptom_check(db, store, sample):
    payload = sample("symptom_check")
    seed.add_visit(db, payload["visit_id"], patient_id=payload["patient_id"])
    raw = json.dumps(payload).encode()
    store.save_symptom_check(payload, raw, NOW, "postgresql")

    [visit] = seed.fetch(db, "SELECT patient_symptoms, symptom_received_at, stage FROM visits")
    assert visit == {"patient_symptoms": payload["patient_symptoms"], "symptom_received_at": NOW,
                     "stage": "received_symptom"}
    [stored] = seed.fetch(db, "SELECT analysis, kind, sha256 FROM record_payloads")
    assert stored == {"analysis": "symptom", "kind": "raw", "sha256": sha256_hex(raw)}
    check = store.stored_symptom_check(payload["visit_id"])
    assert (check.visit_id, check.received_at, check.raw) == (payload["visit_id"], NOW, raw)


def test_stored_symptom_check_missing(db, store):
    seed.add_visit(db, 10)
    with pytest.raises(LookupError):
        store.stored_symptom_check(10)
    with pytest.raises(LookupError):
        store.stored_symptom_check(11)


def test_symptom_failure_rolls_back(db, store):
    seed.add_visit(db, 10)
    with pytest.raises(ValueError):
        store.save_symptom_check({"visit_id": 10, "patient_symptoms": "x"}, b"not json", NOW, "blob")
    assert seed.fetch(db, "SELECT symptom_received_at, stage FROM visits") == [
        {"symptom_received_at": None, "stage": "received"}]


def test_symptoms_waiting_for_processing(db, store):
    for visit_id in (1, 2, 3, 4, 5):
        seed.add_visit(db, visit_id)
    for visit_id, age in ((3, 30), (1, 30), (2, 40), (4, 5), (5, 50)):
        store.save_symptom_check({"visit_id": visit_id, "patient_symptoms": "x"}, b"{}",
                                 NOW - timedelta(minutes=age), "blob")
    seed.add_processing(db, 5, analysis="symptom")                # processed: excluded
    seed.add_processing(db, 3)                                    # only the visit was processed: still waits
    seed.add_visit(db, 6)                                         # no symptom check at all

    assert store.symptoms_waiting_for_processing(NOW - timedelta(minutes=10)) == [2, 1, 3]


def test_save_rejected(db, store):
    errors = [{"field": "visit_id", "code": "missing"}]
    store.save_rejected(4, b"{bad", errors, "rejected/x.json")
    store.save_rejected(None, b"\xff", errors, None)

    rows = seed.fetch(db, "SELECT source_id, raw_bytes, errors, blob_path FROM rejected_payloads ORDER BY rejected_id")
    assert [(r["source_id"], bytes(r["raw_bytes"]), r["errors"], r["blob_path"]) for r in rows] == [
        (4, b"{bad", errors, "rejected/x.json"), (None, b"\xff", errors, None)]
