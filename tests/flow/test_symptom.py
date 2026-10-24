import json
from datetime import UTC, datetime, timedelta

import pytest

from patient360.domain.builders.blocked import BLOCKED_MESSAGES
from patient360.domain.rules.retrieval import CatalogueTest
from patient360.flow.dispatch import Dispatcher
from patient360.flow.intake import Accepted, Intake
from patient360.flow.process import Processor
from patient360.flow.results import OutputReceiver
from patient360.flow.send_result import ResultSender
from patient360.flow.symptom import SymptomProcessor, process_waiting_symptoms
from patient360.prompts import PROMPT_AND_SCHEMA_BY_SOURCE
from tests.fakes import FakeBackend, FakeBlobs, FakeCatalogue, FakeS3, FakeStore

SECRET = b"a-long-test-secret-value"
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
EXTRA_TEST = CatalogueTest(900, "Extra test", "why", "X")
VISIT_ID, PATIENT_ID = 2071739799, 2141109993          # the ids of the symptom_check sample


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


def aws_record(key, text, **extra):
    return {"recordId": key, "type": "message", "role": "assistant", "model": "model-x",
            "content": [{"type": "text", "text": text}], "metadata": {"en": {"usage": {"input_tokens": 1}}}, **extra}


class World:
    def __init__(self, sample, visit_sample="urgent_mild"):
        self.sample, self.store, self.blobs, self.out = sample, FakeStore(), FakeBlobs(), FakeBlobs()
        self.s3, self.clock = FakeS3(), NOW
        self.catalogue = FakeCatalogue(tests_by_category={"X": [EXTRA_TEST]}, names_ar={"Extra test": "AR-Extra test"})
        visit = sample(visit_sample)
        visit.update(visit_id=VISIT_ID, patient_id=PATIENT_ID)
        self.catalogue = FakeCatalogue({p["parameter_id"]: "X" for t in visit["tests"] for p in t["results"]},
                                       {"X": [EXTRA_TEST]}, {"Extra test": "AR-Extra test"})
        Intake(self.store, self.blobs, SECRET, lambda: NOW).receive(json.dumps(visit).encode())
        Processor(self.store, self.catalogue, self.blobs).process(VISIT_ID)
        self.visit = self.store.visits[VISIT_ID]
        self.key = self.visit["idempotency_key"]
        self.source = visit["source"]

    def give_the_visit_its_result(self):
        folder = {1: "urgent", 4: "on-demand"}[self.source]
        path = f"aws-output-merged/{folder}/normal/2026/10/24/{self.key}.json"
        self.out.files[path] = json.dumps(aws_record(self.key, answer())).encode()
        self.receiver().receive(path)

    def receive_the_symptom_check(self, symptoms=None):
        symptom = self.sample("symptom_check")
        if symptoms:
            symptom["patient_symptoms"] = symptoms
        outcome = Intake(self.store, self.blobs, SECRET, lambda: NOW).receive(json.dumps(symptom).encode())
        assert isinstance(outcome, Accepted)

    def receiver(self):
        return OutputReceiver(self.store, self.out, self.catalogue)

    def symptom_processor(self):
        return SymptomProcessor(self.store, self.catalogue, self.blobs)

    def put_symptom_answer(self, record, lane="normal"):
        path = f"aws-output-merged/symptom-checker/{lane}/2026/10/24/{self.key}.json"
        self.out.files[path] = json.dumps(record).encode()
        return path


@pytest.fixture
def world(sample):
    return World(sample)


def patient_data(model_input):
    return json.loads(model_input["messages"][0]["content"][0]["text"])


def test_a_symptom_check_makes_its_own_request_from_the_symptoms_the_results_and_the_previous_answer(world):
    world.give_the_visit_its_result()
    world.receive_the_symptom_check("headache and tiredness for a week")
    done = world.symptom_processor().process(VISIT_ID)
    assert (done.lane, done.read_from) == ("normal", "blob")
    saved = world.store.symptom_checks[VISIT_ID]
    assert saved["stage"] == "processed_symptom"
    data = patient_data(saved["model_input"])
    assert data["source"] == "symptom-checker" and data["patient_symptoms"] == "headache and tiredness for a week"
    assert data["previous_ai_analysis"]["summary"] == "summary" and "packages" not in data["previous_ai_analysis"]
    assert '"ar"' not in json.dumps(data) and data["tests"] and data["retrieved_tests"][0]["test_name"] == "Extra test"
    prompt, schema = PROMPT_AND_SCHEMA_BY_SOURCE["symptom-checker"]
    assert saved["model_input"]["system"][0]["text"] == prompt
    assert saved["model_input"]["output_config"]["format"]["schema"] == schema
    path = f"processed/symptom-checker/normal/2026/10/24/{world.key}.json"
    assert json.loads(world.blobs.files[path]) == saved["model_input"]


def test_the_symptom_request_has_no_ids(world):
    world.give_the_visit_its_result()
    world.receive_the_symptom_check()
    world.symptom_processor().process(VISIT_ID)
    text = json.dumps(world.store.symptom_checks[VISIT_ID]["model_input"])
    assert str(VISIT_ID) not in text and str(PATIENT_ID) not in text


def test_a_critical_visit_sends_its_symptom_check_to_the_critical_lane(sample):
    world = World(sample, "on_demand_critical")
    world.give_the_visit_its_result()
    world.receive_the_symptom_check()
    assert world.symptom_processor().process(VISIT_ID).lane == "critical"
    assert f"processed/symptom-checker/critical/2026/10/24/{world.key}.json" in world.blobs.files


def test_without_a_visit_result_the_symptom_check_waits_and_is_picked_up_later(world):
    world.receive_the_symptom_check()
    processor = world.symptom_processor()
    assert processor.process(VISIT_ID) is None
    assert world.store.symptom_checks[VISIT_ID]["stage"] == "received_symptom"
    later = NOW + timedelta(minutes=6)
    assert process_waiting_symptoms(world.store, processor, later) == []          # the visit still has no result
    world.give_the_visit_its_result()
    [done] = process_waiting_symptoms(world.store, processor, later)
    assert done.visit_id == VISIT_ID and world.store.symptom_checks[VISIT_ID]["stage"] == "processed_symptom"
    assert process_waiting_symptoms(world.store, processor, later) == []          # never again


def test_the_sweep_leaves_a_fresh_symptom_check_alone(world):
    world.give_the_visit_its_result()
    world.receive_the_symptom_check()
    assert process_waiting_symptoms(world.store, world.symptom_processor(), NOW + timedelta(minutes=4)) == []


def test_the_whole_way_the_visit_result_and_the_symptom_result_are_two_posts(world):
    world.give_the_visit_its_result()
    world.receive_the_symptom_check()
    world.symptom_processor().process(VISIT_ID)
    Dispatcher(world.store, world.blobs, world.s3, lambda: NOW).run()
    assert [source for source, _ in world.s3.uploads] == ["symptom-checker"]
    assert world.s3.uploads[0][1] == f"processed/normal/2026/10/24/{world.key}.json"
    path = world.put_symptom_answer(aws_record(world.key, answer(with_symptom_keys=True)))
    assert [r.outcome for r in world.receiver().receive(path)] == ["saved"]
    backend = FakeBackend()
    ResultSender(world.store, backend, lambda: NOW).run()
    assert [post["source"] for post in backend.posts] == [world.source, 3]          # urgent goes before symptom-checker
    visit_post, symptom_post = backend.posts
    assert symptom_post["visit_id"] == VISIT_ID and symptom_post["patient_id"] == PATIENT_ID
    assert symptom_post["ai"]["recommendations"][0]["change_type"] == both("kept")
    assert symptom_post["ai"]["recommendations"][0]["explanation"] == both("still applies")
    assert "because" in symptom_post["html"]["en"] and "AR-because" in symptom_post["html"]["ar"]
    assert "because" not in visit_post["html"]["en"]                                 # the visit html has no reasons
    assert world.visit["stage"] == "delivered" and world.store.symptom_checks[VISIT_ID]["stage"] == "delivered_symptom"


def test_a_symptom_answer_is_not_a_duplicate_of_the_visit_answer_but_arriving_twice_is(world):
    world.give_the_visit_its_result()
    world.receive_the_symptom_check()
    path = world.put_symptom_answer(aws_record(world.key, answer(with_symptom_keys=True)))
    assert [r.outcome for r in world.receiver().receive(path)] == ["saved"]
    assert [r.outcome for r in world.receiver().receive(path)] == ["duplicate"]


def test_a_bad_symptom_answer_fails_only_the_symptom_check_not_the_visit(world):
    world.give_the_visit_its_result()
    world.receive_the_symptom_check()
    path = world.put_symptom_answer(aws_record(world.key, '{"summary": "wrong shape"}'))
    assert [r.outcome for r in world.receiver().receive(path)] == ["invalid_output"]
    assert world.store.symptom_checks[VISIT_ID]["stage"] == "failed_symptom"
    assert world.visit["stage"] == "post_processing"                      # the visit's own result is untouched


def test_a_blocked_symptom_text_gets_the_input_message(world):
    world.give_the_visit_its_result()
    world.receive_the_symptom_check()
    blocked = aws_record(world.key, answer(), **{
        "amazon-bedrock-guardrailAction": "INTERVENED",
        "amazon-bedrock-trace": {"guardrail": {"input": {"id": {"topicPolicy": {"topics": [
            {"name": "x", "type": "DENY", "action": "BLOCKED"}]}}}}}})
    world.receiver().receive(world.put_symptom_answer(blocked))
    public = world.store.symptom_checks[VISIT_ID]["public_output"]
    assert public["source"] == 3 and public["ai"]["summary"] == BLOCKED_MESSAGES["input"]
    assert public["ai"]["recommendations"] is None


def test_a_normal_patients_symptom_check_still_gets_tests_from_the_parameter_categories(sample):
    world = World(sample, "batch_normal")
    world.source = 4
    assert world.visit["tier"] == "NORMAL"
    assert "retrieved_tests" not in patient_data(world.visit["model_input"])           # the visit itself: none
    world.store.visits[VISIT_ID]["public_output"] = {"ai": json.loads(answer())}        # the visit has its result
    world.receive_the_symptom_check()
    world.symptom_processor().process(VISIT_ID)
    data = patient_data(world.store.symptom_checks[VISIT_ID]["model_input"])
    assert [t["test_name"] for t in data["retrieved_tests"]] == ["Extra test"]


def test_a_not_normal_patients_symptom_check_keeps_the_same_rule_as_the_visit(sample):
    world = World(sample, "urgent_mild")
    assert world.visit["tier"] == "MILD"
    world.give_the_visit_its_result()
    world.receive_the_symptom_check()
    world.symptom_processor().process(VISIT_ID)
    data = patient_data(world.store.symptom_checks[VISIT_ID]["model_input"])
    assert [t["test_name"] for t in data["retrieved_tests"]] == ["Extra test"]


def test_a_visit_whose_result_was_blocked_is_never_symptom_checked(world):
    blocked = aws_record(world.key, answer(), **{"amazon-bedrock-guardrailAction": "INTERVENED"})
    path = f"aws-output-merged/urgent/normal/2026/10/24/{world.key}.json"
    world.out.files[path] = json.dumps(blocked).encode()
    world.receiver().receive(path)
    world.receive_the_symptom_check()
    assert world.symptom_processor().process(VISIT_ID) is None
    assert process_waiting_symptoms(world.store, world.symptom_processor(), NOW + timedelta(days=1)) == []
    assert world.store.symptom_checks[VISIT_ID]["stage"] == "received_symptom" and not world.s3.uploads
