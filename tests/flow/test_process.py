import json
from datetime import UTC, datetime, timedelta

import pytest

from patient360.domain.rules.retrieval import CatalogueTest
from patient360.flow.intake import Accepted, Intake
from patient360.flow.process import Processor, process_stuck_visits
from patient360.prompts import PROMPT_AND_SCHEMA_BY_SOURCE
from tests.fakes import FakeBlobs, FakeCatalogue, FakeStore

SECRET = b"a-long-test-secret-value"
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
EXTRA_TEST = CatalogueTest(900, "Extra test", "why")


def receive(sample, name, blobs=None):
    """Receive a sample through the real intake; returns the visit, its key, the store and the blobs."""
    store, blobs = FakeStore(), blobs or FakeBlobs()
    visit = sample(name)
    outcome = Intake(store, blobs, SECRET, lambda: NOW).receive(json.dumps(visit).encode())
    assert isinstance(outcome, Accepted)
    return visit, outcome.idempotency_key, store, blobs


def patient_data(model_input):
    """The patient JSON inside the Bedrock request (the user message)."""
    return json.loads(model_input["messages"][0]["content"][0]["text"])


def catalogue_for(visit):
    ids = {p["parameter_id"]: [1] for t in visit["tests"] for p in t["results"]}
    return FakeCatalogue(ids, {1: [EXTRA_TEST]})


@pytest.mark.parametrize(("name", "tier", "lane", "folder"), [
    ("urgent_mild", "MILD", "normal", "urgent"),
    ("on_demand_critical", "CRITICAL", "critical", "on-demand"),
])
def test_an_urgent_or_on_demand_visit_is_written_to_its_lane_folder(sample, name, tier, lane, folder):
    visit, key, store, blobs = receive(sample, name)
    outcome = Processor(store, catalogue_for(visit), blobs).process(visit["visit_id"])
    assert (outcome.tier, outcome.lane, outcome.read_from) == (tier, lane, "blob")
    written = json.loads(blobs.files[f"processed/{folder}/{lane}/2026/10/24/{key}.json"])
    saved = store.visits[visit["visit_id"]]
    assert written == saved["model_input"] and saved["tier"] == tier and saved["stage"] == "processed"


def test_the_ai_json_has_no_ids_and_carries_the_retrieved_tests(sample):
    visit, _, store, blobs = receive(sample, "on_demand_critical")
    Processor(store, catalogue_for(visit), blobs).process(visit["visit_id"])
    processed = patient_data(store.visits[visit["visit_id"]]["model_input"])
    assert processed["retrieved_tests"] == [{"test_name": "Extra test", "test_description": "why"}]
    assert not any(str(visit[field]) in json.dumps(processed) for field in ("patient_id", "visit_id"))


def test_no_retrieved_tests_key_when_the_catalogue_gives_nothing(sample):
    visit, _, store, blobs = receive(sample, "urgent_mild")
    Processor(store, FakeCatalogue(), blobs).process(visit["visit_id"])
    assert "retrieved_tests" not in patient_data(store.visits[visit["visit_id"]]["model_input"])


def test_a_batch_visit_is_saved_in_the_database_only_until_its_batch_exists(sample):
    visit, _, store, blobs = receive(sample, "batch_normal")
    outcome = Processor(store, catalogue_for(visit), blobs).process(visit["visit_id"])
    assert outcome.read_from == "postgresql" and store.visits[visit["visit_id"]]["stage"] == "processed"
    assert not any(path.startswith("processed/") for path in blobs.files)


def test_a_visit_with_only_descriptive_results_goes_to_the_critical_lane(sample):
    visit, _, store, blobs = receive(sample, "descriptive_only")
    outcome = Processor(store, FakeCatalogue(), blobs).process(visit["visit_id"])
    assert (outcome.tier, outcome.lane) == ("MODERATE", "critical")


def test_when_blob_is_down_the_database_copy_is_used(sample):
    visit, _, store, _ = receive(sample, "urgent_mild")
    outcome = Processor(store, catalogue_for(visit), FakeBlobs(down=True)).process(visit["visit_id"])
    saved = store.visits[visit["visit_id"]]
    assert outcome.read_from == "postgresql" and saved["processed_read_from"] == "postgresql" and saved["model_input"]


@pytest.mark.parametrize(("name", "source"), [("urgent_mild", "urgent"), ("on_demand_critical", "on-demand"),
                                              ("batch_normal", "batch")])
def test_each_source_gets_its_own_prompt_and_schema(sample, name, source):
    visit, _, store, blobs = receive(sample, name)
    Processor(store, catalogue_for(visit), blobs).process(visit["visit_id"])
    model_input = store.visits[visit["visit_id"]]["model_input"]
    prompt, schema = PROMPT_AND_SCHEMA_BY_SOURCE[source]
    assert model_input["system"][0]["text"] == prompt
    assert model_input["output_config"]["format"]["schema"] == schema
    assert patient_data(model_input)["source"] == source



def test_a_visit_accepted_but_never_processed_is_picked_up_after_five_minutes(sample):
    visit, _, store, blobs = receive(sample, "urgent_mild")
    processor = Processor(store, catalogue_for(visit), blobs)
    assert process_stuck_visits(store, processor, NOW + timedelta(minutes=4)) == []       # still being processed
    assert store.visits[visit["visit_id"]]["stage"] == "received"
    [done] = process_stuck_visits(store, processor, NOW + timedelta(minutes=6))
    assert done.visit_id == visit["visit_id"] and store.visits[visit["visit_id"]]["stage"] == "processed"
    assert process_stuck_visits(store, processor, NOW + timedelta(minutes=60)) == []      # never again


def test_the_sweep_takes_the_oldest_first_and_leaves_processed_visits_alone(sample):
    store, blobs = FakeStore(), FakeBlobs()
    intake = Intake(store, blobs, SECRET, lambda: NOW)
    for visit_id, name in ((7, "urgent_mild"), (8, "on_demand_critical")):
        visit = sample(name)
        visit["visit_id"] = visit_id
        intake.receive(json.dumps(visit).encode())
    processor = Processor(store, FakeCatalogue(), blobs)
    processor.process(8)                                                                  # this one was fine
    assert [p.visit_id for p in process_stuck_visits(store, processor, NOW + timedelta(hours=1))] == [7]
