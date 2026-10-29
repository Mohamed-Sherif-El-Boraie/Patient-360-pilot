"""The packages in the result sent to the wrapper: chosen from the patient's sex and age (read from the saved visit) and
the tests the AI recommended."""
import pytest

from patient360.domain.builders.blocked import BLOCKED_MESSAGES
from patient360.domain.rules.packages import Package
from tests.fakes import FakeCatalogue
from tests.flow.test_results import World, record

GLUCOSE_PACKAGE = Package(1, "Glucose check", 3, None, None, frozenset({18}), "فحص الجلوكوز")
NAMES = {18: ("Extra test", "AR-Extra test"), 19: ("Other test", None)}


@pytest.fixture
def world(sample):
    return World(sample)


def received(world, name="urgent_mild", catalogue=None, blocked=False):
    world.catalogue = catalogue or world.catalogue
    [visit] = world.add(name, 1)
    key = visit["idempotency_key"]
    rec = record(key)
    if blocked:
        rec["amazon-bedrock-guardrailAction"] = "INTERVENED"
    world.receiver().receive("urgent", world.write("urgent", f"normal/2026/10/24/{key}/05-output-merged.json", rec))
    return visit["public_output"]


def catalogue(*packages):
    return FakeCatalogue(names_ar={"Extra test": "AR-Extra test"}, packages=list(packages), test_names=NAMES,
                         test_ids_by_name={"extra test": 18, "ar-extra test": 18})


def test_the_package_that_covers_the_recommended_test_is_in_the_result_with_both_languages(world):
    public = received(world, catalogue=catalogue(GLUCOSE_PACKAGE))
    assert public["ai"]["packages"] == [{
        "package_name": {"en": "Glucose check", "ar": "فحص الجلوكوز"},
        "tests": [{"test_name": {"en": "Extra test", "ar": "AR-Extra test"}}]}]


def test_the_sex_of_the_patient_decides_which_packages_fit(world):
    women_only = Package(2, "Women check", 2, None, None, frozenset({18}))
    public = received(world, catalogue=catalogue(women_only))          # urgent_mild is a man (sex 1)
    assert public["ai"]["packages"] == []


def test_the_age_may_arrive_as_text_and_decides_which_packages_fit(sample):
    young_only = Package(3, "Young check", 3, 0, 40, frozenset({18}))
    adult = Package(4, "Adult check", 3, 41, 70, frozenset({18}))
    visit = sample("urgent_mild")
    assert visit["patient"]["age_days"] == 19200                          # 52.6 years
    world = World(lambda name: sample(name) | {"patient": {**sample(name)["patient"], "age_days": "19200"}})
    public = received(world, catalogue=catalogue(young_only, adult))
    assert [p["package_name"]["en"] for p in public["ai"]["packages"]] == ["Adult check"]


def test_a_fully_unmatched_recommendation_still_gets_the_routine_packages(world):
    routine = Package(5, "Routine check", 3, 18, 150, frozenset({19}), None, "WELLNESS")
    public = received(world, catalogue=catalogue(routine))
    assert public["ai"]["packages"][0]["package_name"] == {"en": "Routine check", "ar": "Routine check"}
    assert public["ai"]["packages"][0]["tests"] == [{"test_name": {"en": "Other test", "ar": "Other test"}}]


def test_a_blocked_answer_has_no_packages(world):
    public = received(world, catalogue=catalogue(GLUCOSE_PACKAGE), blocked=True)
    assert "packages" not in public["ai"]
    assert public["ai"]["summary"]["en"] in {m["en"] for m in BLOCKED_MESSAGES.values()}


def test_the_symptom_check_does_not_ask_for_the_tests_it_removed(world):
    world.catalogue = catalogue(GLUCOSE_PACKAGE)
    receiver = world.receiver()
    ai = {"recommendations": [{"test_name": "Extra test", "change_type": {"en": "Removed", "ar": "x"}}]}
    patient = {"age_days": 10000, "sex": 1}
    assert receiver._packages(patient, ai) == []
    ai["recommendations"][0]["change_type"] = {"en": "kept", "ar": "x"}
    assert len(receiver._packages(patient, ai)) == 1
    ai["recommendations"] = [{"test_name": "  EXTRA   test "}]               # case and spaces do not matter
    assert len(receiver._packages(patient, ai)) == 1
