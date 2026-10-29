import json
from datetime import UTC, datetime, timedelta

import pytest

from patient360.domain.builders.backend_output import build_failed_output, for_wrapper
from patient360.domain.builders.blocked import BLOCKED_MESSAGES
from patient360.flow.batching import BatchTrigger
from patient360.flow.intake import Intake
from patient360.flow.process import Processor
from patient360.flow.results import OutputReceiver
from patient360.flow.send_result import ResultSender
from patient360.ports import BackendUnavailable
from tests.fakes import FakeBackend, FakeBlobs, FakeCatalogue, FakeStore, FunctionApp

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


def vpath(key, lane="normal"):
    """A visit answer inside the output container of its source."""
    return f"{lane}/2026/10/24/{key}/05-output-merged.json"


def bpath(batch):
    """The JSONL answer of a batch inside the batch output container."""
    return f"{batch.lane}-jsonls/2026/10/25/{batch.batch_id}/05-output-merged.jsonl"


class World:
    def __init__(self, sample):
        self.sample, self.store, self.blobs = sample, FakeStore(), FakeBlobs()
        self.out = {s: FakeBlobs() for s in ("urgent", "symptom-checker", "on-demand", "batch")}   # AWS's containers
        self.catalogue = FakeCatalogue(names_ar={"Extra test": "AR-Extra test"})

    def add(self, name, visit_id, count=1):
        for i in range(count):
            visit = self.sample(name)
            visit.update(visit_id=visit_id + i, patient_id=7_000_000 + visit_id + i)
            Intake(self.store, self.blobs, SECRET, lambda: NOW).receive(json.dumps(visit).encode())
            Processor(self.store, self.catalogue, self.blobs).process(visit_id + i)
        return [self.store.visits[visit_id + i] for i in range(count)]

    def receiver(self):
        return FunctionApp(OutputReceiver(self.store, self.catalogue), self.out)

    def write(self, source, path, *records):
        self.out[source].files[path] = "\n".join(json.dumps(r) for r in records).encode()
        return path


@pytest.fixture
def world(sample):
    return World(sample)


def test_a_visit_result_is_saved_and_turned_into_the_backend_result(world):
    [visit] = world.add("urgent_mild", 1)
    key = visit["idempotency_key"]
    path = world.write("urgent", vpath(key), record(key))
    assert [r.outcome for r in world.receiver().receive("urgent", path)] == ["saved"]
    assert visit["stage"] == "post_processing" and visit["internal_output"]["model"] == "model-x"
    public = visit["public_output"]
    assert (public["patient_id"], public["visit_id"], public["source"]) == (7_000_001, 1, 1)
    assert public["ai"]["summary"] == both("summary")
    assert public["ai"]["recommendations"][0]["test_name"] == "Extra test"
    assert {"parameter_id", "status"} <= set(public["tests"][0]["results"][0])
    assert "AR-Extra test" in public["html"]["ar"] and "Extra test" in public["html"]["en"]


def test_the_backend_body_is_kept_next_to_the_answer_and_the_record_is_kept_as_received(world):
    [visit] = world.add("urgent_mild", 1)
    key = visit["idempotency_key"]
    kept = FakeBlobs()
    text = json.dumps(record(key), indent=3)           # odd spacing: must come back exactly as it was
    result = OutputReceiver(world.store, world.catalogue, {"urgent": kept}).receive_record("urgent", vpath(key), text)
    assert [r.outcome for r in result] == ["saved"]
    assert json.loads(kept.files[f"normal/2026/10/{'24'}/{key}/06-backend-output.json"]) == for_wrapper(
        visit["public_output"])
    assert visit["internal_raw"] == text.encode()


def test_a_blob_problem_while_keeping_the_backend_body_does_not_lose_the_answer(world):
    [visit] = world.add("urgent_mild", 1)
    key = visit["idempotency_key"]
    result = OutputReceiver(world.store, world.catalogue, {"urgent": FakeBlobs(down=True)}).receive_record(
        "urgent", vpath(key), json.dumps(record(key)))
    assert [r.outcome for r in result] == ["saved"] and visit["public_output"]


def test_nothing_internal_reaches_the_backend_result(world):
    [visit] = world.add("urgent_mild", 1)
    key = visit["idempotency_key"]
    path = world.write("urgent", vpath(key), record(key))
    world.receiver().receive("urgent", path)
    text = json.dumps(visit["public_output"])
    assert not any(word in text for word in ("model-x", "input_tokens", "recordId", "metadata", "stop_reason", key))


def test_the_saved_result_is_then_sent_to_the_backend(world):
    [visit] = world.add("urgent_mild", 1)
    key = visit["idempotency_key"]
    world.receiver().receive("urgent", world.write("urgent", vpath(key), record(key)))
    backend = FakeBackend()
    ResultSender(world.store, backend, lambda: NOW).run()
    assert backend.posts == [for_wrapper(visit["public_output"])] and visit["stage"] == "delivered"


def test_the_backend_never_gets_the_summaries_but_the_saved_result_keeps_them_and_a_retry_is_stripped_too(world):
    [visit] = world.add("urgent_mild", 1)
    key = visit["idempotency_key"]
    world.receiver().receive("urgent", world.write("urgent", vpath(key), record(key)))
    saved = visit["public_output"]
    assert {"summary", "simple_summary", "critical_alerts"} <= set(saved["ai"])      # the symptom check reads these
    sent = []

    class Down(FakeBackend):
        def post_result(self, body):
            sent.append(body)
            if len(sent) == 1:
                raise BackendUnavailable("down")
            super().post_result(body)

    ResultSender(world.store, Down(), lambda: NOW).run()
    ResultSender(world.store, Down(), lambda: NOW + timedelta(days=1)).run()
    assert len(sent) == 2 and sent[0] == sent[1]
    assert set(sent[0]) == {"success", "patient_id", "visit_id", "source", "symptom_flag", "tests", "ai", "html"}
    assert set(sent[0]["ai"]) == {"note", "recommendations", "packages"}
    assert "summary" in sent[0]["html"]["en"]
    result = sent[0]["tests"][0]["results"][0]
    assert list(result) == ["parameter_id", "value", "status", "result_date", "unit", "critical", "history"]
    assert set(result["critical"]) == {"direction"}
    assert {"summary", "simple_summary", "critical_alerts"} <= set(visit["public_output"]["ai"])


def test_the_same_file_arriving_twice_is_saved_once(world):
    [visit] = world.add("urgent_mild", 1)
    key = visit["idempotency_key"]
    path = world.write("urgent", vpath(key), record(key))
    assert world.receiver().receive("urgent", path)[0].outcome == "saved"
    first = dict(visit["public_output"])
    assert world.receiver().receive("urgent", path)[0].outcome == "duplicate" and visit["public_output"] == first


def test_a_batch_file_has_one_result_per_line_and_every_visit_moves_on(world):
    visits = world.add("batch_normal", 100, count=3)
    [batch] = BatchTrigger(world.store, world.blobs, 3).run()
    keys = [v["idempotency_key"] for v in visits]
    path = world.write("batch", bpath(batch), *[record(k) for k in keys])
    assert [r.outcome for r in world.receiver().receive("batch", path)] == ["saved"] * 3
    assert {v["stage"] for v in visits} == {"post_processing"}
    assert world.store.batches[batch.batch_id]["status"] == "completed"
    assert list(world.out["batch"].files) == [path]            # we write nothing back: AWS owns the container
    assert all(v["public_output"]["source"] == 2 for v in visits)

def test_a_batch_is_completed_only_when_every_visit_has_its_output(world):
    visits = world.add("batch_normal", 100, count=3)
    [batch] = BatchTrigger(world.store, world.blobs, 3).run()
    path = bpath(batch)
    receiver = OutputReceiver(world.store, world.catalogue)
    status = lambda: world.store.batches[batch.batch_id]["status"]      # noqa: E731
    for visit, expected in zip(visits, ("partially_completed", "partially_completed", "completed"), strict=True):
        line = json.dumps(record(visit["idempotency_key"]))
        assert receiver.receive_record("batch", path, line)[0].outcome == "saved"      # one record per call
        assert status() == expected
    receiver.receive_record("batch", path, json.dumps(record(visits[0]["idempotency_key"])))      # a resend
    assert status() == "completed"


def test_one_bad_answer_in_a_batch_fails_only_that_visit(world):
    visits = world.add("batch_normal", 100, count=3)
    [batch] = BatchTrigger(world.store, world.blobs, 3).run()
    keys = [v["idempotency_key"] for v in visits]
    broken = record(keys[1], text='{"summary": "not the right shape"}')
    path = world.write("batch", bpath(batch),
                       record(keys[0]), broken, record(keys[2]))
    assert [r.outcome for r in world.receiver().receive("batch", path)] == ["saved", "invalid_output", "saved"]
    assert [v["stage"] for v in visits] == ["post_processing", "failed", "post_processing"]
    assert visits[1]["public_output"]["success"] is False
    assert world.store.batches[batch.batch_id]["status"] == "partially_completed"


def test_a_result_for_a_visit_we_do_not_know_is_ignored_and_saves_nothing(world):
    path = world.write("urgent", vpath("a" * 32), record("a" * 32))
    assert [r.outcome for r in world.receiver().receive("urgent", path)] == ["unknown_visit"]
    assert list(world.out["urgent"].files) == [path]                     # no other file was written


def test_a_result_under_the_wrong_source_is_ignored_and_changes_nothing(world):
    [visit] = world.add("urgent_mild", 1)
    key = visit["idempotency_key"]
    path = world.write("on-demand", vpath(key), record(key))
    assert [r.outcome for r in world.receiver().receive("on-demand", path)] == ["source_mismatch"]
    assert visit["stage"] == "processed" and "public_output" not in visit


def test_a_file_that_is_not_json_at_all_is_ignored(world):
    path = vpath("a" * 32)
    world.out["urgent"].files[path] = b"not json at all"
    assert [r.outcome for r in world.receiver().receive("urgent", path)] == ["invalid_output"]


def test_a_file_with_broken_bytes_does_not_crash_the_receiver(world):
    path = vpath("a" * 32)
    world.out["urgent"].files[path] = b'{"recordId": "\xff\xfe broken"}'
    assert [r.outcome for r in world.receiver().receive("urgent", path)] == ["unknown_visit"]


def test_a_small_json_mistake_is_repaired_and_the_answer_is_still_checked(world):
    [visit] = world.add("urgent_mild", 1)
    key = visit["idempotency_key"]
    path = vpath(key)
    world.out["urgent"].files[path] = (json.dumps(record(key))[:-1] + ",}").encode()          # a trailing comma
    assert [r.outcome for r in world.receiver().receive("urgent", path)] == ["saved"]
    assert visit["public_output"]["ai"]["summary"] == both("summary")


def test_a_file_cut_short_that_repairs_into_a_wrong_answer_still_fails(world):
    [visit] = world.add("urgent_mild", 1)
    key = visit["idempotency_key"]
    path = vpath(key)
    world.out["urgent"].files[path] = json.dumps(record(key))[:80].encode()                # cut off inside the answer
    assert [r.outcome for r in world.receiver().receive("urgent", path)] == ["invalid_output"]
    assert visit["stage"] == "failed" and visit["public_output"]["success"] is False


def test_the_invalid_answer_tells_the_backend_the_patient_failed(world):
    [visit] = world.add("urgent_mild", 1)
    key = visit["idempotency_key"]
    path = world.write("urgent", vpath(key), record(key, text='{"summary": "wrong"}'))
    assert [r.outcome for r in world.receiver().receive("urgent", path)] == ["invalid_output"]
    backend = FakeBackend()
    ResultSender(world.store, backend, lambda: NOW).run()
    assert backend.posts == [build_failed_output(patient_id=7_000_001, visit_id=1, source=1)]
    assert visit["stage"] == "failed"


def only_note(key):
    return record(key, text=json.dumps({"note": both("note"), "recommendations": []}))


def test_a_visit_answer_needs_summary_and_simple_summary_but_a_symptom_answer_does_not(world):
    [visit] = world.add("urgent_mild", 1)
    key = visit["idempotency_key"]
    path = world.write("urgent", vpath(key), only_note(key))
    assert [r.outcome for r in world.receiver().receive("urgent", path)] == ["invalid_output"]
    assert visit["public_output"]["success"] is False


def failed_path(key, lane="normal"):
    return f"{lane}/2026/10/24/{key}/07-failed.json"


def test_a_failed_file_is_recorded_without_reading_it_and_the_wrapper_is_told(world):
    [visit] = world.add("urgent_mild", 1)
    key = visit["idempotency_key"]
    path = world.write("urgent", failed_path(key), {"anything": "at all"})
    world.out["urgent"].files[path] = b"\x00 not json {"             # the body is never read
    assert [r.outcome for r in world.receiver().receive("urgent", path)] == ["failed_recorded"]
    assert visit["stage"] == "failed"
    backend = FakeBackend()
    sent = ResultSender(world.store, backend, lambda: NOW).run()
    assert [d.outcome for d in sent] == ["delivered"]
    assert backend.posts == [build_failed_output(patient_id=7_000_001, visit_id=1, source=1)]
    assert visit["stage"] == "failed"                                  # delivered, and still a failed visit
    assert ResultSender(world.store, backend, lambda: NOW).run() == [] and len(backend.posts) == 1


@pytest.mark.parametrize(("sample_name", "source", "lane"), [
    ("urgent_mild", "urgent", "normal"), ("on_demand_critical", "on-demand", "critical")])
def test_a_failed_file_works_for_every_visit_source(world, sample_name, source, lane):
    [visit] = world.add(sample_name, 1)
    path = failed_path(visit["idempotency_key"], lane)
    assert [r.outcome for r in world.receiver().receive_record(source, path, "")] == ["failed_recorded"]


def test_a_failed_file_in_the_batch_container_fails_that_batch_visit(world):
    [visit] = world.add("batch_normal", 100, count=1)
    path = failed_path(visit["idempotency_key"])
    assert [r.outcome for r in world.receiver().receive_record("batch", path, "")] == ["failed_recorded"]
    assert visit["public_output"]["source"] == 2


def test_a_failed_file_for_an_unknown_visit_or_the_wrong_source_changes_nothing(world):
    [visit] = world.add("urgent_mild", 1)
    receiver = world.receiver()
    assert receiver.receive_record("urgent", failed_path("a" * 32), "")[0].outcome == "unknown_visit"
    wrong = receiver.receive_record("on-demand", failed_path(visit["idempotency_key"]), "")
    assert wrong[0].outcome == "source_mismatch"
    assert visit["stage"] == "processed" and "public_output" not in visit


def test_a_failed_file_after_a_saved_answer_is_a_duplicate_and_a_second_one_changes_nothing(world):
    [first, second] = world.add("urgent_mild", 1) + world.add("urgent_mild", 2)
    answered = world.write("urgent", vpath(first["idempotency_key"]), record(first["idempotency_key"]))
    world.receiver().receive("urgent", answered)
    saved = dict(first["public_output"])
    late = world.receiver().receive_record("urgent", failed_path(first["idempotency_key"]), "")
    assert late[0].outcome == "duplicate"
    assert first["public_output"] == saved and first["stage"] == "post_processing"
    receiver = world.receiver()
    assert receiver.receive_record("urgent", failed_path(second["idempotency_key"]), "")[0].outcome == "failed_recorded"
    assert receiver.receive_record("urgent", failed_path(second["idempotency_key"]), "")[0].outcome == "duplicate"


def test_a_blocked_output_gets_our_own_problem_message_and_nothing_the_model_wrote(world):
    [visit] = world.add("urgent_mild", 1)
    key = visit["idempotency_key"]
    blocked = record(key, text=ai_text("BLOCKED-CONTENT-CANARY"))
    blocked["amazon-bedrock-guardrailAction"] = "INTERVENED"
    path = world.write("urgent", vpath(key), blocked)
    assert [r.outcome for r in world.receiver().receive("urgent", path)] == ["blocked"]
    public = visit["public_output"]
    assert visit["stage"] == "post_processing" and "BLOCKED-CONTENT-CANARY" not in json.dumps(public)
    assert public["ai"] == {"summary": BLOCKED_MESSAGES["output"], "simple_summary": None,
                            "critical_alerts": None, "note": None, "recommendations": None}
    assert BLOCKED_MESSAGES["output"]["en"] in public["html"]["en"]
    assert BLOCKED_MESSAGES["output"]["ar"] in public["html"]["ar"]
    assert public["tests"] and public["patient_id"] == 7_000_001      # our own classification is still sent
    backend = FakeBackend()
    ResultSender(world.store, backend, lambda: NOW).run()
    [posted] = backend.posts
    assert posted["ai"] == {"note": BLOCKED_MESSAGES["output"], "recommendations": [], "packages": []}
    assert posted["symptom_flag"] is False
    assert posted["tests"] == public["tests"] and BLOCKED_MESSAGES["output"]["en"] in posted["html"]["en"]


def test_a_blocked_input_gets_the_input_message(world):
    [visit] = world.add("urgent_mild", 1)
    key = visit["idempotency_key"]
    blocked = {**record(key), "amazon-bedrock-guardrailAction": "INTERVENED",
               "amazon-bedrock-trace": {"guardrail": {"input": {"id": {"wordPolicy": {
                   "customWords": [{"match": "x", "action": "BLOCKED"}]}}}}}}
    path = world.write("urgent", vpath(key), blocked)
    world.receiver().receive("urgent", path)
    assert visit["public_output"]["ai"]["summary"] == BLOCKED_MESSAGES["input"]


def test_a_blocked_result_inside_a_batch_does_not_stop_the_others(world):
    visits = world.add("batch_normal", 100, count=2)
    [batch] = BatchTrigger(world.store, world.blobs, 2).run()
    keys = [v["idempotency_key"] for v in visits]
    blocked = {**record(keys[0]), "amazon-bedrock-guardrailAction": "INTERVENED"}
    path = world.write("batch", bpath(batch),
                       blocked, record(keys[1]))
    assert [r.outcome for r in world.receiver().receive("batch", path)] == ["blocked", "saved"]
    assert world.store.batches[batch.batch_id]["status"] == "completed"


@pytest.mark.parametrize(("source", "path"), [
    ("batch", "normal/2026/10/25/" + "a" * 32 + "/05-output-merged.json"),   # a visit file inside the batch container
    ("urgent", "normal/2026/10/24/" + "a" * 32 + "/04-output-en.json"),      # another phase file of AWS
    ("urgent", "normal/2026/10/24/readme.txt"),
    ("urgent", "normal-jsonls/2026/10/25/normal_000001/05-output-merged.jsonl"),   # a JSONL outside the batch container
    ("urgent", "somewhere/else.json"),
])
def test_files_that_are_not_a_visit_result_are_ignored(world, source, path):
    world.out[source].files[path] = b"{}"
    assert [r.outcome for r in world.receiver().receive(source, path)] == ["ignored"]


@pytest.mark.parametrize(("sample_name", "folder", "flag"), [
    ("batch_normal", None, False),            # fully normal: no retrieved tests, the symptom checker stays closed
    ("urgent_mild", "urgent", True),
    ("on_demand_critical", "on-demand", True),
])
def test_the_symptom_flag_follows_the_patient(world, sample_name, folder, flag):
    [visit] = world.add(sample_name, 1)
    if folder is None:           # a batch visit comes back inside a batch file
        [batch] = BatchTrigger(world.store, world.blobs, 1).run()
        folder, path = "batch", bpath(batch)
    else:
        path = vpath(visit["idempotency_key"], "critical" if folder == "on-demand" else "normal")
    world.write(folder, path, record(visit["idempotency_key"]))
    world.receiver().receive(folder, path)
    assert visit["public_output"]["symptom_flag"] is flag


def test_a_blocked_result_closes_the_symptom_checker(world):
    [visit] = world.add("urgent_mild", 1)
    key = visit["idempotency_key"]
    blocked = {**record(key), "amazon-bedrock-guardrailAction": "INTERVENED"}
    world.receiver().receive("urgent", world.write("urgent", vpath(key), blocked))
    assert visit["public_output"]["symptom_flag"] is False



def test_an_intervention_that_only_anonymized_data_keeps_the_model_answer(world):
    [visit] = world.add("urgent_mild", 1)
    key = visit["idempotency_key"]
    anonymized = {**record(key), "amazon-bedrock-guardrailAction": "INTERVENED",
                  "amazon-bedrock-trace": {"guardrail": {"outputs": [{"id": {"sensitiveInformationPolicy": {
                      "piiEntities": [{"type": "NAME", "action": "ANONYMIZED"}]}}}]}}}
    path = world.write("urgent", vpath(key), anonymized)
    assert [r.outcome for r in world.receiver().receive("urgent", path)] == ["saved"]
    assert visit["public_output"]["ai"]["summary"] == both("summary")
