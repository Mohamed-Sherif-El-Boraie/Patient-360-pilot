import json
from datetime import UTC, datetime

import pytest

from patient360.domain.builders.blocked import BLOCKED_MESSAGES
from patient360.flow.batching import BatchTrigger
from patient360.flow.intake import Intake
from patient360.flow.process import Processor
from patient360.flow.results import OutputReceiver
from patient360.flow.send_result import ResultSender
from tests.fakes import FakeBackend, FakeBlobs, FakeCatalogue, FakeStore

SECRET = b"a-long-test-secret-value"
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)


def both(english):
    return {"en": english, "ar": f"AR-{english}"}


def ai_text(test_name="Extra test"):
    return json.dumps({
        "summary": both("summary"), "simple_summary": both("simple"), "critical_alerts": [both("alert")],
        "note": both("note"),
        "recommendations": [{"test_name": test_name, "priority": both("important"), "reason": both("why"),
                             "timing": both("soon"), "guideline": both("guide"),
                             "advisory": {"level": both("none"), "message": both("fine")}}]})


def record(key, text=None):
    """What AWS writes to Blob 2 (phase 7): the model answer plus internal model and usage details."""
    usage = {"stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 20}}
    return {"recordId": key, "type": "message", "role": "assistant", "model": "model-x",
            "content": [{"type": "text", "text": text or ai_text()}], "metadata": {"en": usage, "ar": usage}}


class World:
    def __init__(self, sample):
        self.sample, self.store, self.blobs, self.out = sample, FakeStore(), FakeBlobs(), FakeBlobs()
        self.catalogue = FakeCatalogue(names_ar={"Extra test": "AR-Extra test"})

    def add(self, name, visit_id, count=1):
        for i in range(count):
            visit = self.sample(name)
            visit.update(visit_id=visit_id + i, patient_id=7_000_000 + visit_id + i)
            Intake(self.store, self.blobs, SECRET, lambda: NOW).receive(json.dumps(visit).encode())
            Processor(self.store, self.catalogue, self.blobs).process(visit_id + i)
        return [self.store.visits[visit_id + i] for i in range(count)]

    def receiver(self):
        return OutputReceiver(self.store, self.out, self.catalogue)

    def write(self, path, *records):
        self.out.files[path] = "\n".join(json.dumps(r) for r in records).encode()
        return path


@pytest.fixture
def world(sample):
    return World(sample)


def test_a_visit_result_is_saved_and_turned_into_the_backend_result(world):
    [visit] = world.add("urgent_mild", 1)
    key = visit["idempotency_key"]
    path = world.write(f"aws-output-merged/urgent/normal/2026/10/24/{key}.json", record(key))
    assert [r.outcome for r in world.receiver().receive(path)] == ["saved"]
    assert visit["stage"] == "post_processing" and visit["internal_output"]["model"] == "model-x"
    public = visit["public_output"]
    assert (public["patient_id"], public["visit_id"], public["source"]) == (7_000_001, 1, 1)
    assert public["ai"]["summary"] == both("summary")
    assert public["ai"]["recommendations"][0]["test_name"] == "Extra test"
    assert {"parameter_id", "status"} <= set(public["tests"][0]["results"][0])
    assert "AR-Extra test" in public["html"]["ar"] and "Extra test" in public["html"]["en"]


def test_nothing_internal_reaches_the_backend_result(world):
    [visit] = world.add("urgent_mild", 1)
    key = visit["idempotency_key"]
    path = world.write(f"aws-output-merged/urgent/normal/2026/10/24/{key}.json", record(key))
    world.receiver().receive(path)
    text = json.dumps(visit["public_output"])
    assert not any(word in text for word in ("model-x", "input_tokens", "recordId", "metadata", "stop_reason", key))


def test_the_saved_result_is_then_sent_to_the_backend(world):
    [visit] = world.add("urgent_mild", 1)
    key = visit["idempotency_key"]
    world.receiver().receive(world.write(f"aws-output-merged/urgent/normal/2026/10/24/{key}.json", record(key)))
    backend = FakeBackend()
    ResultSender(world.store, backend, lambda: NOW).run()
    assert backend.posts == [visit["public_output"]] and visit["stage"] == "delivered"


def test_the_same_file_arriving_twice_is_saved_once(world):
    [visit] = world.add("urgent_mild", 1)
    key = visit["idempotency_key"]
    path = world.write(f"aws-output-merged/urgent/normal/2026/10/24/{key}.json", record(key))
    assert world.receiver().receive(path)[0].outcome == "saved"
    first = dict(visit["public_output"])
    assert world.receiver().receive(path)[0].outcome == "duplicate" and visit["public_output"] == first


def test_a_batch_file_is_split_and_every_visit_moves_on(world):
    visits = world.add("batch_normal", 100, count=3)
    [batch] = BatchTrigger(world.store, world.blobs, 3).run()
    keys = [v["idempotency_key"] for v in visits]
    day = "2026/10/25"
    path = world.write(f"aws-output-merged/batch/{batch.lane}-jsonls/{day}/{batch.batch_id}.jsonl",
                       *[record(k) for k in keys])
    assert [r.outcome for r in world.receiver().receive(path)] == ["saved"] * 3
    assert {v["stage"] for v in visits} == {"post_processing"}
    assert world.store.batches[batch.batch_id]["status"] == "completed"
    for key in keys:
        split = f"aws-output-merged/batch/{batch.lane}/{day}/{batch.batch_id}/{key}.json"
        assert json.loads(world.out.files[split])["recordId"] == key
    assert all(v["public_output"]["source"] == 2 for v in visits)




def test_one_bad_answer_in_a_batch_fails_only_that_visit(world):
    visits = world.add("batch_normal", 100, count=3)
    [batch] = BatchTrigger(world.store, world.blobs, 3).run()
    keys = [v["idempotency_key"] for v in visits]
    broken = record(keys[1], text='{"summary": "not the right shape"}')
    path = world.write(f"aws-output-merged/batch/{batch.lane}-jsonls/2026/10/25/{batch.batch_id}.jsonl",
                       record(keys[0]), broken, record(keys[2]))
    assert [r.outcome for r in world.receiver().receive(path)] == ["saved", "invalid_output", "saved"]
    assert [v["stage"] for v in visits] == ["post_processing", "failed", "post_processing"]
    assert visits[1]["output_error"] == "invalid_ai_output"
    assert world.store.batches[batch.batch_id]["status"] == "partially_completed"


def test_a_result_for_a_visit_we_do_not_know_is_ignored_and_saves_nothing(world):
    path = world.write("aws-output-merged/urgent/normal/2026/10/24/" + "a" * 32 + ".json", record("a" * 32))
    assert [r.outcome for r in world.receiver().receive(path)] == ["unknown_visit"]
    assert list(world.out.files) == [path]                      # no other file was written


def test_a_result_under_the_wrong_source_is_ignored_and_changes_nothing(world):
    [visit] = world.add("urgent_mild", 1)
    key = visit["idempotency_key"]
    path = world.write(f"aws-output-merged/on-demand/normal/2026/10/24/{key}.json", record(key))
    assert [r.outcome for r in world.receiver().receive(path)] == ["source_mismatch"]
    assert visit["stage"] == "processed" and "public_output" not in visit


def test_a_file_that_is_not_json_at_all_is_ignored(world):
    path = "aws-output-merged/urgent/normal/2026/10/24/" + "a" * 32 + ".json"
    world.out.files[path] = b"not json at all"
    assert [r.outcome for r in world.receiver().receive(path)] == ["invalid_output"]


def test_a_file_with_broken_bytes_does_not_crash_the_receiver(world):
    path = "aws-output-merged/urgent/normal/2026/10/24/" + "a" * 32 + ".json"
    world.out.files[path] = b'{"recordId": "\xff\xfe broken"}'
    assert [r.outcome for r in world.receiver().receive(path)] == ["unknown_visit"]


def test_a_small_json_mistake_is_repaired_and_the_answer_is_still_checked(world):
    [visit] = world.add("urgent_mild", 1)
    key = visit["idempotency_key"]
    path = f"aws-output-merged/urgent/normal/2026/10/24/{key}.json"
    world.out.files[path] = (json.dumps(record(key))[:-1] + ",}").encode()          # a trailing comma
    assert [r.outcome for r in world.receiver().receive(path)] == ["saved"]
    assert visit["public_output"]["ai"]["summary"] == both("summary")


def test_a_file_cut_short_that_repairs_into_a_wrong_answer_still_fails(world):
    [visit] = world.add("urgent_mild", 1)
    key = visit["idempotency_key"]
    path = f"aws-output-merged/urgent/normal/2026/10/24/{key}.json"
    world.out.files[path] = json.dumps(record(key))[:80].encode()                    # cut off inside the answer
    assert [r.outcome for r in world.receiver().receive(path)] == ["invalid_output"]
    assert visit["stage"] == "failed" and "public_output" not in visit


def test_a_blocked_output_gets_our_own_problem_message_and_nothing_the_model_wrote(world):
    [visit] = world.add("urgent_mild", 1)
    key = visit["idempotency_key"]
    blocked = record(key, text=ai_text("BLOCKED-CONTENT-CANARY"))
    blocked["amazon-bedrock-guardrailAction"] = "INTERVENED"
    path = world.write(f"aws-output-merged/urgent/normal/2026/10/24/{key}.json", blocked)
    assert [r.outcome for r in world.receiver().receive(path)] == ["blocked"]
    public = visit["public_output"]
    assert visit["stage"] == "post_processing" and "BLOCKED-CONTENT-CANARY" not in json.dumps(public)
    assert public["ai"] == {"summary": BLOCKED_MESSAGES["output"], "simple_summary": None,
                            "critical_alerts": None, "note": None, "recommendations": None}
    assert BLOCKED_MESSAGES["output"]["en"] in public["html"]["en"]
    assert BLOCKED_MESSAGES["output"]["ar"] in public["html"]["ar"]
    assert public["tests"] and public["patient_id"] == 7_000_001      # our own classification is still sent
    backend = FakeBackend()
    ResultSender(world.store, backend, lambda: NOW).run()
    assert backend.posts == [public]


def test_a_blocked_input_gets_the_input_message(world):
    [visit] = world.add("urgent_mild", 1)
    key = visit["idempotency_key"]
    blocked = {**record(key), "amazon-bedrock-guardrailAction": "INTERVENED",
               "amazon-bedrock-trace": {"guardrail": {"input": {"id": {"wordPolicy": {
                   "customWords": [{"match": "x", "action": "BLOCKED"}]}}}}}}
    path = world.write(f"aws-output-merged/urgent/normal/2026/10/24/{key}.json", blocked)
    world.receiver().receive(path)
    assert visit["public_output"]["ai"]["summary"] == BLOCKED_MESSAGES["input"]


def test_a_blocked_result_inside_a_batch_does_not_stop_the_others(world):
    visits = world.add("batch_normal", 100, count=2)
    [batch] = BatchTrigger(world.store, world.blobs, 2).run()
    keys = [v["idempotency_key"] for v in visits]
    blocked = {**record(keys[0]), "amazon-bedrock-guardrailAction": "INTERVENED"}
    path = world.write(f"aws-output-merged/batch/{batch.lane}-jsonls/2026/10/25/{batch.batch_id}.jsonl",
                       blocked, record(keys[1]))
    assert [r.outcome for r in world.receiver().receive(path)] == ["blocked", "saved"]
    assert world.store.batches[batch.batch_id]["status"] == "completed"


@pytest.mark.parametrize("path", [
    "aws-output-merged/batch/normal/2026/10/25/normal_000001/" + "a" * 32 + ".json",    # our own split file
    "aws-output-merged/symptom-checker/normal/2026/10/24/" + "a" * 32 + ".json",        # the symptom flow (Task 7)
    "aws-output-merged/urgent/normal/2026/10/24/readme.txt",
    "somewhere/else.json",
])
def test_files_that_are_not_a_visit_result_are_ignored(world, path):
    world.out.files[path] = b"{}"
    assert world.receiver().receive(path) == []


def test_a_file_that_is_not_there_is_ignored(world):
    assert world.receiver().receive("aws-output-merged/urgent/normal/2026/10/24/" + "a" * 32 + ".json") == []


def test_an_intervention_that_only_anonymized_data_keeps_the_model_answer(world):
    [visit] = world.add("urgent_mild", 1)
    key = visit["idempotency_key"]
    anonymized = {**record(key), "amazon-bedrock-guardrailAction": "INTERVENED",
                  "amazon-bedrock-trace": {"guardrail": {"outputs": [{"id": {"sensitiveInformationPolicy": {
                      "piiEntities": [{"type": "NAME", "action": "ANONYMIZED"}]}}}]}}}
    path = world.write(f"aws-output-merged/urgent/normal/2026/10/24/{key}.json", anonymized)
    assert [r.outcome for r in world.receiver().receive(path)] == ["saved"]
    assert visit["public_output"]["ai"]["summary"] == both("summary")
