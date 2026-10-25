"""The phase 7 samples (tests/fixtures/samples/*_phase7.json): the answer AWS writes to Blob 2, English and Arabic.

They must be a valid answer, match their input sample, and flow through the receiver into the Backend result.
"""
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from patient360.domain.common.ids import make_idempotency_key
from patient360.domain.payload.ai_answer import AiAnswer
from patient360.flow.batching import BatchTrigger
from patient360.flow.intake import Accepted, Intake
from patient360.flow.process import Processor
from patient360.flow.results import OutputReceiver
from tests.fakes import FakeBlobs, FakeCatalogue, FakeStore

SAMPLES = Path(__file__).parents[1] / "fixtures" / "samples"
SECRET = b"a-long-test-secret-value"
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
ARABIC = re.compile(r"[؀-ۿ]")
VISITS = ["batch_normal", "urgent_mild", "batch_critical", "on_demand_critical", "descriptive_only"]
SYMPTOM_FLAG = {"batch_normal": False, "urgent_mild": True, "batch_critical": True, "on_demand_critical": True,
                "descriptive_only": True}      # false only for the fully normal patient


def phase7(name):
    return json.loads((SAMPLES / f"{name}_phase7.json").read_text(encoding="utf-8"))


def answer_of(record):
    return json.loads(record["content"][0]["text"])


def every_pair(node):
    """Every {"en": ..., "ar": ...} in an answer."""
    if isinstance(node, dict):
        if set(node) == {"en", "ar"}:
            yield node
        else:
            for child in node.values():
                yield from every_pair(child)
    elif isinstance(node, list):
        for child in node:
            yield from every_pair(child)


@pytest.mark.parametrize("name", [*VISITS, "symptom_check"])
def test_a_phase7_sample_is_a_valid_bilingual_answer_for_its_own_visit(sample, name):
    record, source = phase7(name), sample(name)
    key = make_idempotency_key(SECRET, patient_id=source["patient_id"], visit_id=source["visit_id"])
    assert record["recordId"] == key and record["role"] == "assistant" and record["model"].startswith("claude-")
    assert set(record["metadata"]) == {"en", "ar"}
    answer = AiAnswer.model_validate_json(record["content"][0]["text"])
    assert len(answer.recommendations) <= 6
    for pair in every_pair(answer_of(record)):
        assert pair["en"] and not ARABIC.search(pair["en"]), pair
        assert pair["ar"] and ARABIC.search(pair["ar"]) or pair["ar"] == pair["en"] == "KDIGO", pair


def test_the_samples_tell_a_story_that_matches_their_results():
    assert answer_of(phase7("batch_normal"))["critical_alerts"] == []
    assert answer_of(phase7("batch_normal"))["recommendations"] == []
    assert len(answer_of(phase7("batch_critical"))["critical_alerts"]) == 2
    assert answer_of(phase7("batch_critical")) == answer_of(phase7("on_demand_critical"))
    priorities = {r["priority"]["en"] for r in answer_of(phase7("batch_critical"))["recommendations"]}
    assert priorities == {"urgent", "important"}
    changes = [r["change_type"]["en"] for r in answer_of(phase7("symptom_check"))["recommendations"]]
    assert sorted(set(changes)) == ["added", "kept", "removed"]
    assert all("explanation" in r for r in answer_of(phase7("symptom_check"))["recommendations"])
    assert all("change_type" not in r for r in answer_of(phase7("batch_critical"))["recommendations"])


class Rig:
    def __init__(self, sample):
        self.sample, self.store, self.blobs, self.out = sample, FakeStore(), FakeBlobs(), FakeBlobs()
        self.receiver = OutputReceiver(self.store, self.out, FakeCatalogue())

    def receive_visit(self, name):
        body = json.dumps(self.sample(name)).encode()
        assert isinstance(Intake(self.store, self.blobs, SECRET, lambda: NOW).receive(body), Accepted)
        visit_id = self.sample(name)["visit_id"]
        done = Processor(self.store, FakeCatalogue(), self.blobs).process(visit_id)
        return self.store.visits[visit_id], done.lane

    def deliver(self, name, path):
        record = (SAMPLES / f"{name}_phase7.json").read_text(encoding="utf-8")
        if path.endswith(".jsonl"):                     # a JSONL file has one record per line
            record = json.dumps(json.loads(record), ensure_ascii=False)
        self.out.files[path] = record.encode()
        return self.receiver.receive(path)


@pytest.mark.parametrize("name", VISITS)
def test_a_phase7_sample_becomes_the_backend_result_with_the_right_symptom_flag(sample, name):
    rig = Rig(sample)
    visit, lane = rig.receive_visit(name)
    key = visit["idempotency_key"]
    if name.startswith("batch"):
        [batch] = BatchTrigger(rig.store, rig.blobs, 1).run()
        path = f"aws-output-merged/batch/{batch.lane}-jsonls/2026/10/25/{batch.batch_id}.jsonl"
    else:
        folder = "urgent" if name == "urgent_mild" else "on-demand"
        path = f"aws-output-merged/{folder}/{lane}/2026/10/24/{key}.json"
    assert [r.outcome for r in rig.deliver(name, path)] == ["saved"]
    public = visit["public_output"]
    assert public["symptom_flag"] is SYMPTOM_FLAG[name]
    assert public["ai"]["summary"] == answer_of(phase7(name))["summary"]
    assert answer_of(phase7(name))["summary"]["ar"] in public["html"]["ar"]
    assert "claude-" not in json.dumps(public) and "usage" not in json.dumps(public)


def test_the_symptom_sample_becomes_a_symptom_result_with_the_extra_keys_and_a_closed_flag(sample):
    rig = Rig(sample)
    visit, lane = rig.receive_visit("batch_critical")
    rig.store.save_symptom_check(sample("symptom_check"), b"{}", NOW, "blob")
    key = visit["idempotency_key"]
    path = f"aws-output-merged/symptom-checker/{lane}/2026/10/24/{key}.json"
    assert [r.outcome for r in rig.deliver("symptom_check", path)] == ["saved"]
    public = rig.store.symptom_checks[visit["payload"]["visit_id"]]["public_output"]
    assert public["source"] == 3 and public["symptom_flag"] is False
    assert {r["change_type"]["en"] for r in public["ai"]["recommendations"]} == {"kept", "added", "removed"}
    assert "Uric acid" in public["html"]["en"] and "Parathyroid" in public["html"]["en"]
    assert "Haemoglobin A1C" not in public["html"]["en"]    # the removed test is not shown


def catalogue_rows():
    return json.loads((SAMPLES / "catalogue_rows.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", [*VISITS, "symptom_check"])
def test_the_sample_tests_and_parameters_are_real_rows_of_the_catalogue_sheet(sample, name):
    rows = catalogue_rows()
    source = sample(name)
    for test in [*source.get("tests", []), *source.get("descriptive_results", [])]:
        assert rows["tests"][str(test["test_id"])]["test_name"] == test["test_name"]
        for result in test.get("results", []):
            assert rows["parameters"][str(result["parameter_id"])]["parameter_name"] == result["parameter_name"]


@pytest.mark.parametrize("name", [*VISITS, "symptom_check"])
def test_every_recommended_test_is_a_catalogue_test_in_a_category_of_the_visit(sample, name):
    rows = catalogue_rows()
    source = sample("batch_critical" if name == "symptom_check" else name)      # symptom check: no results of its own
    by_name = {t["test_name"]: t for t in rows["tests"].values()}
    performed = {t["test_name"] for t in source.get("tests", [])}
    visit_categories = {c for t in source.get("tests", []) for r in t["results"]
                        for c in rows["parameters"][str(r["parameter_id"])]["category_id"]}
    for item in answer_of(phase7(name))["recommendations"]:
        test = by_name[item["test_name"]]
        assert item["test_name"] not in performed
        assert set(test["category_id"]) & visit_categories, item["test_name"]
