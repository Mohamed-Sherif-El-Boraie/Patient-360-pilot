"""The PostgreSQL side of saving an AI result and delivering it to the Backend."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from patient360.adapters.postgres.outputs import OutputsMixin
from patient360.ports import DeliveryUnit
from tests.adapters.postgres import seed
from tests.adapters.postgres.seed import fetch

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


class Store(OutputsMixin):
    def __init__(self, db):
        self._db = db


def en_ar(text):
    return {"en": text, "ar": f"ar {text}"}


def recommendation(test_name="CBC", priority="Urgent", level="discuss_with_doctor", **extra):
    return {"test_name": test_name, "priority": en_ar(priority), "reason": en_ar("reason"), "timing": en_ar("now"),
            "guideline": en_ar("guide"), "advisory": {"level": en_ar(level), "message": en_ar("talk")}, **extra}


def public_output(visit_id, recommendations=None, source=1):
    ai = {"summary": en_ar("summary"), "simple_summary": en_ar("simple"), "critical_alerts": [en_ar("a1"), en_ar("a2")],
          "note": en_ar("note"),
          "recommendations": [recommendation()] if recommendations is None else recommendations}
    return {"patient_id": 1, "visit_id": visit_id, "source": source, "tests": [], "ai": ai,
            "html": {"en": "<p>en</p>", "ar": "<p>ar</p>"}}


def internal_output(with_arabic=True):
    usage = {"input_tokens": 10, "output_tokens": 20, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4}
    metadata = {"en": {"stop_reason": "end_turn", "stop_sequence": None, "usage": usage}}
    if with_arabic:
        metadata["ar"] = {"stop_reason": "max_tokens", "usage": {"input_tokens": 5, "output_tokens": 6}}
    return {"recordId": "key", "model": "model-x", "content": [{"text": "{}"}], "metadata": metadata}


def blocked_output(visit_id):
    result = public_output(visit_id)
    result["ai"] = {"summary": en_ar("blocked"), "simple_summary": None, "critical_alerts": None, "note": None,
                    "recommendations": None}
    return result


@pytest.fixture
def store(db):
    return Store(db)


@pytest.fixture
def ready(db):
    """A visit (id 1, source urgent) with the tests CBC (active) and Old Test (inactive), as save_output needs."""
    seed.add_visit(db, 1)
    seed.add_test(db, 10, "CBC", name_ar="cbc ar")
    seed.add_test(db, 11, "Old Test", active=False)
    return 1


def processing_row(db, visit_id=1, analysis="visit"):
    return fetch(db, "SELECT * FROM record_processing WHERE visit_id = %s AND analysis = %s", (visit_id, analysis))[0]


def stage_of(db, visit_id=1):
    return fetch(db, "SELECT stage FROM visits WHERE visit_id = %s", (visit_id,))[0]["stage"]


# ---- save_output ------------------------------------------------------------------------------
def test_save_output_writes_every_row_of_a_visit_result(db, store, ready):
    seed.add_processing(db, 1, status="sent")
    result = public_output(1, [recommendation("CBC"), recommendation("Old Test", "Weird", level="bogus")])
    store.save_output(1, internal_output(), result)

    output = fetch(db, "SELECT * FROM ai_outputs")[0]
    assert (output["status"], output["analysis"]) == ("success", "visit")
    assert (output["summary_en"], output["summary_ar"]) == ("summary", "ar summary")
    assert (output["simple_summary_ar"], output["note_en"]) == ("ar simple", "note")
    assert (output["html_en"], output["html_ar"]) == ("<p>en</p>", "<p>ar</p>")
    assert output["generated_at"] is not None
    assert [a["alert_en"] for a in fetch(db, "SELECT * FROM ai_critical_alerts ORDER BY 1")] == ["a1", "a2"]

    first, second = fetch(db, "SELECT * FROM ai_recommendations ORDER BY test_id")
    assert (first["test_id"], first["priority"], first["priority_ar"]) == (10, "urgent", "ar Urgent")
    assert (first["advisory_level"], first["advisory_message_en"], first["change_type"]) == (
        "discuss_with_doctor", "talk", None)
    assert (first["reason_ar"], first["timing_en"], first["guideline_ar"]) == ("ar reason", "now", "ar guide")
    assert (second["test_id"], second["priority"], second["advisory_level"]) == (11, "important", None)

    payloads = fetch(db, "SELECT kind, version, body FROM record_payloads ORDER BY kind")
    assert [(p["kind"], p["version"]) for p in payloads] == [("output_internal", 1), ("output_public", 1)]
    assert payloads[1]["body"] == result

    row = processing_row(db)
    assert (row["step"], row["status"], row["attempts"], row["claimed_by"]) == ("deliver", "pending", 0, None)
    assert stage_of(db) == "post_processing"


def test_model_calls_with_and_without_the_arabic_metadata(db, store, ready):
    store.save_output(1, internal_output(), public_output(1))
    analysis, translation = fetch(db, "SELECT * FROM ai_model_calls ORDER BY purpose")
    assert (analysis["model_id"], analysis["mode"], analysis["input_tokens"], analysis["cache_read_input_tokens"]) == (
        "model-x", "on_demand", 10, 4)
    assert (analysis["stop_reason"], analysis["stop_sequence"]) == ("end_turn", None)
    assert (translation["purpose"], translation["stop_reason"], translation["cache_read_input_tokens"]) == (
        "translation", "max_tokens", None)

    store.save_output(1, internal_output(with_arabic=False), public_output(1))
    assert [c["purpose"] for c in fetch(db, "SELECT purpose FROM ai_model_calls")] == ["analysis"]


def test_no_model_means_no_model_calls(db, store, ready):
    internal = internal_output()
    del internal["model"]
    store.save_output(1, internal, public_output(1))
    assert fetch(db, "SELECT * FROM ai_model_calls") == []


def test_a_batch_visit_gets_the_batch_inference_mode(db, store):
    seed.add_visit(db, 2, source_id=2)
    store.save_output(2, internal_output(), public_output(2, [], source=2))
    assert {c["mode"] for c in fetch(db, "SELECT mode FROM ai_model_calls")} == {"batch_inference"}


def test_a_recommendation_with_an_unknown_test_is_skipped(db, store, ready):
    seed.add_test(db, 12, "Vitamin  D")
    result = public_output(1, [recommendation("Nothing Like This"), recommendation(" vitamin d ")])
    store.save_output(1, internal_output(), result)
    assert [r["test_id"] for r in fetch(db, "SELECT test_id FROM ai_recommendations")] == [12]
    assert fetch(db, "SELECT body FROM record_payloads WHERE kind = 'output_public'")[0]["body"] == result


def test_a_blocked_result_is_saved_with_its_summary_only(db, store, ready):
    store.save_output(1, internal_output(), blocked_output(1))
    output = fetch(db, "SELECT * FROM ai_outputs")[0]
    assert (output["status"], output["summary_en"]) == ("guardrail_blocked", "blocked")
    assert output["simple_summary_en"] is None
    assert fetch(db, "SELECT * FROM ai_critical_alerts") == []
    assert fetch(db, "SELECT * FROM ai_recommendations") == []


def test_save_output_of_a_symptom_check(db, store, ready):
    seed.add_processing(db, 1, analysis="symptom", status="sent")
    seed.add_processing(db, 1, status="delivered", step="deliver")
    result = public_output(1, [recommendation(change_type=en_ar("Added"), explanation=en_ar("why"))], source=3)
    store.save_output(1, internal_output(), result, "symptom")

    assert fetch(db, "SELECT status FROM ai_outputs WHERE analysis = 'symptom'") == [{"status": "answered"}]
    rec = fetch(db, "SELECT * FROM ai_recommendations WHERE analysis = 'symptom'")[0]
    assert (rec["change_type"], rec["change_type_ar"], rec["explanation_en"]) == ("added", "ar Added", "why")
    assert {c["mode"] for c in fetch(db, "SELECT mode FROM ai_model_calls")} == {"on_demand"}
    assert {p["analysis"] for p in fetch(db, "SELECT analysis FROM record_payloads")} == {"symptom"}
    assert processing_row(db, analysis="symptom")["step"] == "deliver"
    assert processing_row(db)["status"] == "delivered"      # the visit's own row is not touched
    assert stage_of(db) == "post_processing_symptom"


def test_saving_twice_replaces_the_rows_and_keeps_the_payload_history(db, store, ready):
    store.save_output(1, internal_output(), public_output(1))
    store.save_output(1, internal_output(), public_output(1, [recommendation("Old Test")]))
    assert [r["test_id"] for r in fetch(db, "SELECT test_id FROM ai_recommendations")] == [11]
    assert len(fetch(db, "SELECT * FROM ai_critical_alerts")) == 2
    assert len(fetch(db, "SELECT * FROM ai_model_calls")) == 2
    assert [p["version"] for p in fetch(db, "SELECT version FROM record_payloads WHERE kind = 'output_public' "
                                            "ORDER BY version")] == [1, 2]
    assert len(fetch(db, "SELECT * FROM record_processing")) == 1


def test_a_failing_save_leaves_nothing(db, store, ready):
    seed.add_processing(db, 1, status="sent")
    broken = public_output(1, [{"test_name": "CBC", "priority": en_ar("Urgent")}])      # no reason: fails mid-way
    with pytest.raises(KeyError):
        store.save_output(1, internal_output(), broken)
    for table in ("ai_outputs", "ai_critical_alerts", "ai_recommendations", "ai_model_calls", "record_payloads"):
        assert fetch(db, f"SELECT * FROM {table}") == []
    assert processing_row(db)["step"] == "upload"
    assert stage_of(db) == "received"


# ---- has_output, mark_output_failed, visit_ai_answer -----------------------------------------
def test_has_output(db, store, ready):
    assert not store.has_output(1)
    store.save_output(1, internal_output(), public_output(1))
    assert store.has_output(1)
    assert not store.has_output(1, "symptom")


def test_mark_output_failed(db, store, ready):
    seed.add_processing(db, 1, status="sent")
    store.mark_output_failed(1, "invalid_ai_output")
    store.mark_output_failed(1, "invalid_ai_output")      # twice is fine
    assert fetch(db, "SELECT status FROM ai_outputs") == [{"status": "error"}]
    assert store.has_output(1)
    row = processing_row(db)
    assert (row["status"], row["last_error"]) == ("failed", "invalid_ai_output")
    assert stage_of(db) == "failed"


def test_mark_output_failed_without_a_work_row(db, store, ready):
    store.mark_output_failed(1, "invalid_ai_output", "symptom")
    assert fetch(db, "SELECT * FROM record_processing") == []
    assert fetch(db, "SELECT analysis FROM ai_outputs") == [{"analysis": "symptom"}]
    assert stage_of(db) == "failed_symptom"


def test_visit_ai_answer(db, store, ready):
    assert store.visit_ai_answer(1) is None
    symptom = public_output(1, [])
    symptom["ai"]["summary"] = en_ar("symptom summary")
    store.save_output(1, internal_output(), symptom, "symptom")
    assert store.visit_ai_answer(1) is None      # a symptom answer is not the visit's answer
    store.save_output(1, internal_output(), public_output(1))
    assert store.visit_ai_answer(1) == public_output(1)["ai"]


def test_set_batch_status(db, store):
    seed.add_batch(db, "normal_000001", status="submitted")
    store.set_batch_status("normal_000001", "partially_completed")
    assert fetch(db, "SELECT status FROM batches") == [{"status": "partially_completed"}]


# ---- claim_next_delivery -----------------------------------------------------------------------
def deliverable(db, visit_id, *, source_id=1, analysis="visit", created_at=seed.LONG_AGO, **processing):
    """A visit whose result waits to be delivered."""
    if not fetch(db, "SELECT 1 FROM visits WHERE visit_id = %s", (visit_id,)):
        seed.add_visit(db, visit_id, source_id=source_id, created_at=created_at)
    seed.add_processing(db, visit_id, analysis=analysis, step="deliver", **processing)


def test_claim_takes_the_best_source_then_the_oldest(db, store):
    day = timedelta(days=1)
    deliverable(db, 1, source_id=2)
    deliverable(db, 2, source_id=4, created_at=seed.LONG_AGO + day)
    deliverable(db, 3, source_id=4)
    deliverable(db, 4, source_id=1, created_at=seed.LONG_AGO + 2 * day)
    deliverable(db, 5, source_id=1, analysis="symptom")      # symptom-checker ranks below urgent, above on-demand
    order = [store.claim_next_delivery(NOW) for _ in range(6)]
    assert [(u.visit_id, u.source) for u in order[:5]] == [
        (4, "urgent"), (5, "symptom-checker"), (3, "on-demand"), (2, "on-demand"), (1, "batch")]
    assert order[0] == DeliveryUnit(4, "urgent", 0, "visit")
    assert order[1].analysis == "symptom"
    assert order[5] is None


def test_a_symptom_unit_is_ordered_by_when_its_check_arrived(db, store):
    for visit_id in (1, 2):
        deliverable(db, visit_id, analysis="symptom")
    with db.transaction() as conn:
        conn.execute("UPDATE visits SET symptom_received_at = %s WHERE visit_id = 1", (NOW,))
        conn.execute("UPDATE visits SET symptom_received_at = %s WHERE visit_id = 2", (NOW - timedelta(hours=1),))
    assert store.claim_next_delivery(NOW).visit_id == 2


def test_claim_marks_the_unit_processing_and_never_gives_it_twice(db, store):
    deliverable(db, 1, attempts=2)
    unit = store.claim_next_delivery(NOW)
    assert unit == DeliveryUnit(1, "urgent", 2, "visit")
    row = processing_row(db)
    assert (row["status"], row["claimed_by"], row["updated_at"]) == ("processing", "urgent", NOW)
    assert store.claim_next_delivery(NOW) is None


def test_a_unit_waiting_for_its_retry_time_is_skipped(db, store):
    deliverable(db, 1, next_retry_at=NOW + timedelta(minutes=5))
    assert store.claim_next_delivery(NOW) is None
    assert store.claim_next_delivery(NOW + timedelta(minutes=5)).visit_id == 1


def test_a_stale_processing_unit_is_claimable_again_but_a_fresh_one_is_not(db, store):
    deliverable(db, 1, status="processing", updated_at=NOW - timedelta(minutes=11))
    deliverable(db, 2, status="processing", updated_at=NOW - timedelta(minutes=1))
    assert store.claim_next_delivery(NOW).visit_id == 1
    assert store.claim_next_delivery(NOW) is None


def test_upload_rows_and_finished_rows_are_never_claimed(db, store):
    seed.add_visit(db, 1)
    seed.add_processing(db, 1, step="upload")
    deliverable(db, 2, status="delivered")
    deliverable(db, 3, status="failed")
    assert store.claim_next_delivery(NOW) is None


def test_two_sessions_cannot_claim_the_same_unit(db, store):
    deliverable(db, 1)
    with db.transaction() as first:
        assert first.execute("SELECT 1 FROM record_processing WHERE visit_id = 1 FOR UPDATE").fetchone()
        assert store.claim_next_delivery(NOW) is None      # locked by the first session: skipped, not waited for


# ---- public_output_of, mark_delivered, mark_delivery_failed_attempt --------------------------------
def test_public_output_of_returns_the_current_version_of_that_analysis(db, store, ready):
    visit = public_output(1)
    store.save_output(1, internal_output(), visit)
    newer = public_output(1, [])
    store.save_output(1, internal_output(), newer)
    symptom = public_output(1, [], source=3)
    store.save_output(1, internal_output(), symptom, "symptom")
    assert store.public_output_of(DeliveryUnit(1, "urgent", 0)) == newer
    assert store.public_output_of(DeliveryUnit(1, "symptom-checker", 0, "symptom")) == symptom


def test_mark_delivered(db, store, ready):
    deliverable(db, 1, status="processing", attempts=1)
    store.mark_delivered(DeliveryUnit(1, "urgent", 1))
    assert processing_row(db)["status"] == "delivered"
    assert stage_of(db) == "delivered"
    attempt = fetch(db, "SELECT * FROM record_processing_attempts")[0]
    assert (attempt["attempt"], attempt["outcome"], attempt["analysis"]) == (2, "sent", "visit")


def test_mark_delivered_of_a_symptom_check(db, store, ready):
    deliverable(db, 1, analysis="symptom", status="processing")
    store.mark_delivered(DeliveryUnit(1, "symptom-checker", 0, "symptom"))
    assert processing_row(db, analysis="symptom")["status"] == "delivered"
    assert stage_of(db) == "delivered_symptom"


def test_a_failed_delivery_is_released_for_the_retry_time(db, store, ready):
    deliverable(db, 1, status="processing")
    retry_at = NOW + timedelta(minutes=2)
    store.mark_delivery_failed_attempt(DeliveryUnit(1, "urgent", 0), "backend_unavailable", retry_at)
    row = processing_row(db)
    assert (row["status"], row["step"], row["attempts"], row["claimed_by"]) == ("pending", "deliver", 1, None)
    assert (row["last_error"], row["next_retry_at"]) == ("backend_unavailable", retry_at)
    assert stage_of(db) == "received"      # the stage only changes when we give up
    attempt = fetch(db, "SELECT * FROM record_processing_attempts")[0]
    assert (attempt["attempt"], attempt["outcome"], attempt["error"]) == (1, "retry", "backend_unavailable")
    assert store.claim_next_delivery(NOW) is None
    assert store.claim_next_delivery(retry_at).attempts == 1


def test_giving_up_a_delivery_fails_the_unit(db, store, ready):
    deliverable(db, 1, analysis="symptom", status="processing", attempts=4)
    store.mark_delivery_failed_attempt(DeliveryUnit(1, "symptom-checker", 4, "symptom"), "backend_rejected", None)
    row = processing_row(db, analysis="symptom")
    assert (row["status"], row["attempts"], row["next_retry_at"]) == ("failed", 5, None)
    assert stage_of(db) == "failed_symptom"
    attempt = fetch(db, "SELECT * FROM record_processing_attempts")[0]
    assert (attempt["attempt"], attempt["outcome"]) == (5, "failed")
    assert store.claim_next_delivery(NOW + timedelta(days=1)) is None
