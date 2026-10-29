import json
from datetime import UTC, datetime, timedelta

import pytest

from patient360.domain.builders.blocked import BLOCKED_MESSAGES
from patient360.domain.builders.html_report import FOOTER, SYMPTOM_MESSAGE, VISIT_MESSAGE
from patient360.domain.rules.packages import Package
from patient360.domain.rules.retrieval import CatalogueTest
from patient360.flow.dispatch import Dispatcher
from patient360.flow.intake import Accepted, Intake
from patient360.flow.process import Processor
from patient360.flow.results import OutputReceiver
from patient360.flow.send_result import ResultSender
from patient360.flow.symptom import SymptomProcessor, process_waiting_symptoms
from patient360.prompts import PROMPT_AND_SCHEMA_BY_SOURCE
from tests.fakes import FakeBackend, FakeBlobs, FakeCatalogue, FakeS3, FakeStore, FunctionApp

SECRET = b"a-long-test-secret-value"
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
EXTRA_TEST = CatalogueTest(900, "Extra test", "why")
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
        self.sample, self.store, self.blobs = sample, FakeStore(), FakeBlobs()
        self.out = {s: FakeBlobs() for s in ("urgent", "symptom-checker", "on-demand", "batch")}
        self.s3, self.clock = FakeS3(), NOW
        self.catalogue = FakeCatalogue(tests_by_category={1: [EXTRA_TEST]}, names_ar={"Extra test": "AR-Extra test"})
        visit = sample(visit_sample)
        visit.update(visit_id=VISIT_ID, patient_id=PATIENT_ID)
        self.catalogue = FakeCatalogue({p["parameter_id"]: [1] for t in visit["tests"] for p in t["results"]},
                                       {1: [EXTRA_TEST]}, {"Extra test": "AR-Extra test"})
        Intake(self.store, self.blobs, SECRET, lambda: NOW).receive(json.dumps(visit).encode())
        Processor(self.store, self.catalogue, self.blobs).process(VISIT_ID)
        self.visit = self.store.visits[VISIT_ID]
        self.key = self.visit["idempotency_key"]
        self.source = visit["source"]

    def give_the_visit_its_result(self):
        folder = {1: "urgent", 4: "on-demand"}[self.source]
        path = f"normal/2026/10/24/{self.key}/05-output-merged.json"
        self.out[folder].files[path] = json.dumps(aws_record(self.key, answer())).encode()
        self.receiver().receive(folder, path)

    def receive_the_symptom_check(self, symptoms=None):
        symptom = self.sample("symptom_check")
        if symptoms:
            symptom["patient_symptoms"] = symptoms
        outcome = Intake(self.store, self.blobs, SECRET, lambda: NOW).receive(json.dumps(symptom).encode())
        assert isinstance(outcome, Accepted)

    def receiver(self):
        return FunctionApp(OutputReceiver(self.store, self.catalogue), self.out)

    def symptom_processor(self):
        return SymptomProcessor(self.store, self.blobs)

    def put_symptom_answer(self, record, lane="normal"):
        path = f"{lane}/2026/10/24/{self.key}/05-output-merged.json"
        self.out["symptom-checker"].files[path] = json.dumps(record).encode()
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
    assert "simple_summary" in data["previous_ai_analysis"]      # the full saved answer, not the stripped one
    assert '"ar"' not in json.dumps(data) and data["tests"] and data["retrieved_tests"][0]["test_name"] == "Extra test"
    prompt, schema = PROMPT_AND_SCHEMA_BY_SOURCE["symptom-checker"]
    assert saved["model_input"]["system"][0]["text"] == prompt
    assert saved["model_input"]["output_config"]["format"]["schema"] == schema
    path = f"processed/symptom-checker/normal/2026/10/24/{world.key}.json"
    assert json.loads(world.blobs.files[path]) == {"recordId": world.key, "modelInput": saved["model_input"]}
    assert "recordId" not in saved["model_input"]


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
    assert world.s3.uploads[0][1] == f"processed/normal/2026/10/24/00-input/{world.key}.json"
    path = world.put_symptom_answer(aws_record(world.key, answer(with_symptom_keys=True)))
    assert [r.outcome for r in world.receiver().receive("symptom-checker", path)] == ["saved"]
    backend = FakeBackend()
    ResultSender(world.store, backend, lambda: NOW).run()
    assert [post["source"] for post in backend.posts] == [world.source, 3]          # urgent goes before symptom-checker
    visit_post, symptom_post = backend.posts
    assert visit_post["symptom_flag"] is True and symptom_post["symptom_flag"] is True
    assert list(symptom_post) == ["success", "patient_id", "visit_id", "source", "symptom_flag", "ai", "html"]
    assert list(symptom_post["ai"]) == ["note", "recommendations", "packages"]
    assert world.store.symptom_checks[VISIT_ID]["public_output"]["symptom_flag"] is True
    assert symptom_post["visit_id"] == VISIT_ID and symptom_post["patient_id"] == PATIENT_ID
    assert symptom_post["ai"]["recommendations"][0]["change_type"] == both("kept")
    assert symptom_post["ai"]["recommendations"][0]["explanation"] == both("still applies")
    assert "because" in symptom_post["html"]["en"] and "AR-because" in symptom_post["html"]["ar"]
    assert "because" not in visit_post["html"]["en"]                                 # the visit html has no reasons
    assert world.visit["stage"] == "delivered" and world.store.symptom_checks[VISIT_ID]["stage"] == "delivered_symptom"


def test_the_symptom_html_ends_with_the_symptom_and_its_date_in_the_saved_result_and_the_post(world):
    world.give_the_visit_its_result()
    world.receive_the_symptom_check("<b>cough</b> & fever")
    calls = []
    original = world.store.stored_symptom_check
    world.store.stored_symptom_check = lambda visit_id: calls.append(visit_id) or original(visit_id)
    path = world.put_symptom_answer(aws_record(world.key, answer(with_symptom_keys=True)))
    world.receiver().receive("symptom-checker", path)
    backend = FakeBackend()
    ResultSender(world.store, backend, lambda: NOW).run()
    posted = next(p for p in backend.posts if p["source"] == 3)["html"]
    saved = world.store.symptom_checks[VISIT_ID]["public_output"]["html"]
    for html in (saved, posted):
        for lang in ("en", "ar"):
            message = SYMPTOM_MESSAGE[lang].format(date="2026-10-24", symptom="&lt;b&gt;cough&lt;/b&gt; &amp; fever")
            assert html[lang].index("</ul>") < html[lang].index(message)
            assert html[lang].endswith(f"<p>{message}</p><p><small>{FOOTER[lang]}</small></p></div>")
    assert calls == [VISIT_ID]


def test_the_visit_html_ends_with_the_visit_message_and_never_reads_the_symptom_check(world):
    world.receive_the_symptom_check("a secret symptom")
    calls = []
    world.store.stored_symptom_check = lambda visit_id: calls.append(visit_id)
    world.give_the_visit_its_result()
    html = world.visit["public_output"]["html"]
    for lang in ("en", "ar"):
        assert html[lang].endswith(f"<p>{VISIT_MESSAGE[lang]}</p><p><small>{FOOTER[lang]}</small></p></div>")
        assert "a secret symptom" not in html[lang]
    assert calls == []


def test_a_blocked_visit_html_has_no_message(world):
    blocked = aws_record(world.key, answer(), **{"amazon-bedrock-guardrailAction": "INTERVENED"})
    path = f"normal/2026/10/24/{world.key}/05-output-merged.json"
    world.out["urgent"].files[path] = json.dumps(blocked).encode()
    world.receiver().receive("urgent", path)
    html = world.visit["public_output"]["html"]
    assert all(VISIT_MESSAGE[lang] not in html[lang] and "<ul>" not in html[lang] for lang in ("en", "ar"))
    assert all(FOOTER[lang] in html[lang] for lang in ("en", "ar"))      # the footer is always there


def test_a_symptom_answer_is_not_a_duplicate_of_the_visit_answer_but_arriving_twice_is(world):
    world.give_the_visit_its_result()
    world.receive_the_symptom_check()
    path = world.put_symptom_answer(aws_record(world.key, answer(with_symptom_keys=True)))
    assert [r.outcome for r in world.receiver().receive("symptom-checker", path)] == ["saved"]
    assert [r.outcome for r in world.receiver().receive("symptom-checker", path)] == ["duplicate"]


def test_a_bad_symptom_answer_fails_only_the_symptom_check_not_the_visit(world):
    world.give_the_visit_its_result()
    world.receive_the_symptom_check()
    path = world.put_symptom_answer(aws_record(world.key, '{"summary": "wrong shape"}'))
    assert [r.outcome for r in world.receiver().receive("symptom-checker", path)] == ["invalid_output"]
    assert world.store.symptom_checks[VISIT_ID]["stage"] == "failed_symptom"
    assert world.visit["stage"] == "post_processing"                      # the visit's own result is untouched


def test_a_blocked_symptom_text_gets_the_input_message(world):
    world.give_the_visit_its_result()
    world.receive_the_symptom_check()
    blocked = aws_record(world.key, answer(), **{
        "amazon-bedrock-guardrailAction": "INTERVENED",
        "amazon-bedrock-trace": {"guardrail": {"input": {"id": {"topicPolicy": {"topics": [
            {"name": "x", "type": "DENY", "action": "BLOCKED"}]}}}}}})
    world.receiver().receive("symptom-checker", world.put_symptom_answer(blocked))
    public = world.store.symptom_checks[VISIT_ID]["public_output"]
    assert public["source"] == 3 and public["ai"]["summary"] == BLOCKED_MESSAGES["input"]
    assert public["ai"]["recommendations"] is None
    backend = FakeBackend()
    ResultSender(world.store, backend, lambda: NOW).run()
    blocked_post = next(p for p in backend.posts if p["source"] == 3)
    assert blocked_post["ai"] == {"note": BLOCKED_MESSAGES["input"], "recommendations": [], "packages": []}
    assert blocked_post["symptom_flag"] is True and "tests" not in blocked_post


def test_the_symptom_packages_are_recomputed_from_the_kept_and_added_tests_and_not_the_removed_one(world):
    world.catalogue = FakeCatalogue(
        packages=[Package(1, "Extra pack", 3, None, None, frozenset({900}), "AR-Extra pack"),
                  Package(2, "Gone pack", 3, None, None, frozenset({901}), "AR-Gone pack")],
        test_ids_by_name={"extra test": 900, "gone test": 901}, test_names={900: ("Extra test", None),
                                                                           901: ("Gone test", None)})
    world.give_the_visit_its_result()
    world.receive_the_symptom_check()
    recs = [{"test_name": name, "priority": both("important"), "reason": both("r"), "timing": both("soon"),
             "guideline": both("g"), "advisory": {"level": both("none"), "message": both("fine")},
             "change_type": both(change), "explanation": both("e")}
            for name, change in (("Extra test", "added"), ("Gone test", "removed"))]
    text = json.dumps({"note": both("note"), "recommendations": recs})
    path = world.put_symptom_answer(aws_record(world.key, text))
    world.receiver().receive("symptom-checker", path)
    assert [p["package_name"]["en"] for p in world.store.symptom_checks[VISIT_ID]["public_output"]["ai"]["packages"]] \
        == ["Extra pack"]


def test_a_normal_visit_has_no_retrieved_tests_so_its_symptom_check_has_none(sample):
    world = World(sample, "batch_normal")
    assert world.visit["tier"] == "NORMAL"
    assert "retrieved_tests" not in patient_data(world.visit["model_input"])
    world.store.visits[VISIT_ID]["public_output"] = {"ai": json.loads(answer())}        # the visit has its result
    world.receive_the_symptom_check()
    world.symptom_processor().process(VISIT_ID)
    assert "retrieved_tests" not in patient_data(world.store.symptom_checks[VISIT_ID]["model_input"])


def test_the_symptom_check_reuses_the_tests_the_visit_got_and_does_not_look_in_the_catalogue(sample):
    world = World(sample, "urgent_mild")
    world.give_the_visit_its_result()
    world.receive_the_symptom_check()
    visit_tests = [{"test_name": "Kept from the visit", "test_description": "d"}]
    visit_data = {**patient_data(world.visit["model_input"]), "retrieved_tests": visit_tests}
    world.visit["model_input"]["messages"][0]["content"][0]["text"] = json.dumps(visit_data)

    SymptomProcessor(world.store, world.blobs).process(VISIT_ID)
    data = patient_data(world.store.symptom_checks[VISIT_ID]["model_input"])
    assert data["retrieved_tests"] == visit_tests


def test_a_symptom_answer_with_only_note_and_recommendations_is_saved(world):
    world.give_the_visit_its_result()
    world.receive_the_symptom_check()
    text = json.dumps({"note": both("note"), "recommendations": []})
    path = world.put_symptom_answer(aws_record(world.key, text))
    assert [r.outcome for r in world.receiver().receive("symptom-checker", path)] == ["saved"]


def test_a_failed_file_of_the_symptom_checker_fails_the_symptom_check_and_tells_the_wrapper(world):
    world.give_the_visit_its_result()
    world.receive_the_symptom_check()
    receiver = world.receiver()
    path = f"normal/2026/10/24/{world.key}/07-failed.json"
    assert receiver.receive_record("symptom-checker", path, "")[0].outcome == "failed_recorded"
    assert receiver.receive_record("symptom-checker", path, "")[0].outcome == "duplicate"
    assert world.store.symptom_checks[VISIT_ID]["stage"] == "failed_symptom"
    backend = FakeBackend()
    ResultSender(world.store, backend, lambda: NOW).run()
    failed = [p for p in backend.posts if p["success"] is False]
    assert failed == [{"success": False, "patient_id": PATIENT_ID, "visit_id": VISIT_ID, "source": 3,
                       "symptom_flag": None, "tests": None, "ai": None, "html": None}]


def test_a_symptom_check_that_arrived_before_its_visit_result_was_blocked_is_never_processed(world):
    """Intake refuses a symptom check once the result is known to be blocked; this is the check that arrived earlier."""
    world.receive_the_symptom_check()                      # accepted: the visit has no result yet, so it waits
    blocked = aws_record(world.key, answer(), **{"amazon-bedrock-guardrailAction": "INTERVENED"})
    path = f"normal/2026/10/24/{world.key}/05-output-merged.json"
    world.out["urgent"].files[path] = json.dumps(blocked).encode()
    world.receiver().receive("urgent", path)
    assert world.symptom_processor().process(VISIT_ID) is None
    assert process_waiting_symptoms(world.store, world.symptom_processor(), NOW + timedelta(days=1)) == []
    assert world.store.symptom_checks[VISIT_ID]["stage"] == "received_symptom" and not world.s3.uploads
