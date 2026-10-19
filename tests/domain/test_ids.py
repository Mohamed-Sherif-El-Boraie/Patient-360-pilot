from patient360.domain.ids import make_record_id

KEY = b"test-key"
BASE = {"patient_id": 7, "visit_id": 9, "source": "urgent"}


def record_id(**changes):
    return make_record_id(KEY, **{**BASE, **changes})


def test_the_same_visit_gives_the_same_id_and_it_is_opaque():
    rid = record_id()
    assert rid == record_id() and len(rid) == 32
    assert "|" not in rid and "7" * 3 not in rid


def test_another_visit_patient_or_key_gives_a_new_id():
    assert record_id(visit_id=10) != record_id()
    assert record_id(patient_id=8) != record_id()
    assert make_record_id(b"other-key", **BASE) != record_id()


def test_the_symptom_check_of_a_visit_is_a_different_record_from_the_visit_itself():
    assert record_id(source="symptom-checker") != record_id(source="urgent")


def test_a_second_symptom_check_for_the_same_visit_is_the_same_record():
    assert record_id(source="symptom-checker") == record_id(source="symptom-checker")
