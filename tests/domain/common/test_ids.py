from patient360.domain.common.ids import make_idempotency_key

KEY = b"test-key"
BASE = {"patient_id": 7, "visit_id": 9, "company_id": 10}


def idempotency_key(**changes):
    return make_idempotency_key(KEY, **{**BASE, **changes})


def test_the_same_visit_gives_the_same_key_and_it_is_opaque():
    value = idempotency_key()
    assert value == idempotency_key() and len(value) == 32
    assert "|" not in value and "7" * 3 not in value


def test_another_visit_patient_or_secret_gives_a_new_key():
    assert idempotency_key(visit_id=10) != idempotency_key()
    assert idempotency_key(patient_id=8) != idempotency_key()
    assert idempotency_key(company_id=15) != idempotency_key()      # same patient and visit, another company
    assert make_idempotency_key(b"other-key", **BASE) != idempotency_key()
