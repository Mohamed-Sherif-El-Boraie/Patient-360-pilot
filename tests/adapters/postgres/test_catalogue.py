from __future__ import annotations

from patient360.adapters.postgres.catalogue import PostgresCatalogue
from patient360.domain.rules.retrieval import select_retrieved_tests

from . import seed


def _ids(tests) -> list[int]:
    return sorted(t.test_id for t in tests)


def test_maps_for_a_small_catalogue(db):
    seed.add_category(db, 1, "Lipids")
    seed.add_category(db, 2, "Heart")
    seed.add_parameter(db, 10, "LDL", categories=(1, 2))
    seed.add_test(db, 100, "Lipid panel", description="fats", categories=(1, 2))
    seed.add_test(db, 101, "ECG", categories=(2,))
    catalogue = PostgresCatalogue(db)

    assert catalogue.parameter_categories() == {10: (1, 2)}
    by_category = catalogue.tests_by_category()
    assert _ids(by_category[1]) == [100]
    assert _ids(by_category[2]) == [100, 101]
    assert by_category[1][0].test_description_en == "fats"
    assert by_category[1][0].category_ids == (1, 2)

    rows = [{"parameter_id": 10, "status": "high", "value": "5"}]
    names = [t["test_name"] for t in select_retrieved_tests(rows, catalogue.parameter_categories(), by_category, ())]
    assert names == ["Lipid panel", "ECG"]


def test_inactive_category_is_ignored(db):
    seed.add_category(db, 1, "Old", active=False)
    seed.add_category(db, 2, "Live")
    seed.add_parameter(db, 10, "P", categories=(1, 2))
    seed.add_test(db, 100, "T", categories=(1, 2))
    seed.add_test(db, 101, "Only old", categories=(1,))
    catalogue = PostgresCatalogue(db)

    assert catalogue.parameter_categories() == {10: (2,)}
    assert {k: _ids(v) for k, v in catalogue.tests_by_category().items()} == {2: [100]}


def test_inactive_test_never_appears(db):
    seed.add_category(db, 1, "C")
    seed.add_test(db, 100, "Gone", categories=(1,), active=False)
    seed.add_test(db, 101, "Kept", categories=(1,))

    assert _ids(PostgresCatalogue(db).tests_by_category()[1]) == [101]


def test_inactive_parameter_has_no_entry(db):
    seed.add_category(db, 1, "C")
    seed.add_parameter(db, 10, "Off", categories=(1,), active=False)
    seed.add_parameter(db, 11, "On", categories=(1,))

    assert PostgresCatalogue(db).parameter_categories() == {11: (1,)}


def test_unknown_category_id_is_ignored(db):
    seed.add_category(db, 1, "C")
    seed.add_parameter(db, 10, "P", categories=(1, 99))
    seed.add_test(db, 100, "T", categories=(99, 1))
    catalogue = PostgresCatalogue(db)

    assert catalogue.parameter_categories() == {10: (1,)}
    assert list(catalogue.tests_by_category()) == [1]


def test_empty_tables_give_empty_maps(db):
    catalogue = PostgresCatalogue(db)

    assert catalogue.parameter_categories() == {}
    assert catalogue.tests_by_category() == {}
    assert catalogue.test_names_ar() == {}


def test_test_names_ar_includes_inactive_and_skips_missing(db):
    seed.add_test(db, 1, "Glucose", name_ar="سكر")
    seed.add_test(db, 2, "Old test", name_ar="قديم", active=False)
    seed.add_test(db, 3, "No Arabic")
    seed.add_test(db, 4, "Empty Arabic", name_ar="")

    assert PostgresCatalogue(db).test_names_ar() == {"Glucose": "سكر", "Old test": "قديم"}


def test_end_to_end_retrieval(db):
    seed.add_category(db, 1, "Kidney")
    seed.add_category(db, 2, "Liver")
    seed.add_category(db, 3, "Retired", active=False)
    seed.add_parameter(db, 10, "Creatinine", categories=(1,))
    seed.add_parameter(db, 11, "ALT", categories=(2, 3))
    seed.add_parameter(db, 12, "Off", categories=(2,), active=False)
    seed.add_test(db, 100, "Urea", categories=(1,))
    seed.add_test(db, 101, "Kidney scan", categories=(1, 2))
    seed.add_test(db, 102, "Retired test", categories=(1,), active=False)
    seed.add_test(db, 103, "Liver panel", categories=(2, 3))
    seed.add_test(db, 104, "Retired only", categories=(3,))
    catalogue = PostgresCatalogue(db)
    rows = [
        {"parameter_id": 10, "status": "critical", "value": "9"},
        {"parameter_id": 11, "status": "high", "value": "80"},
        {"parameter_id": 12, "status": "high", "value": "1"},
    ]

    result = select_retrieved_tests(rows, catalogue.parameter_categories(), catalogue.tests_by_category(), ())

    assert [t["test_name"] for t in result] == ["Urea", "Kidney scan", "Liver panel"]
