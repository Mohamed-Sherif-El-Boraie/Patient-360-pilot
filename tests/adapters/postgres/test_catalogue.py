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


def _add_package(db, package_id, name, *, tests=(), active=True, sex=3, age=(None, None), kind="GENERAL", name_ar=None):
    with db.transaction() as conn:
        conn.execute("INSERT INTO packages (package_id, package_name_en, package_name_ar, package_type, sex_id, "
                     "age_min, age_max, active) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                     (package_id, name, name_ar, kind, sex, age[0], age[1], active))
        for test_id in tests:
            conn.execute("INSERT INTO test_package_map (package_id, test_id) VALUES (%s, %s)", (package_id, test_id))


def test_packages_come_with_their_active_tests_and_only_active_packages_are_read(db):
    seed.add_test(db, 1, "Urea", name_ar="اليوريا")
    seed.add_test(db, 2, "Old test", active=False)
    _add_package(db, 10, "Kidney check", tests=(1, 2), name_ar="فحص الكلى", sex=2, age=(18, 40), kind="WELLNESS")
    _add_package(db, 11, "Empty", tests=())
    _add_package(db, 12, "Switched off", tests=(1,), active=False)

    packages = {p.package_id: p for p in PostgresCatalogue(db).packages()}
    assert sorted(packages) == [10, 11]
    kidney = packages[10]
    assert (kidney.name, kidney.name_ar) == ("Kidney check", "فحص الكلى")
    assert (kidney.package_type, kidney.sex_id) == ("WELLNESS", 2)
    assert (kidney.age_min, kidney.age_max, kidney.test_ids) == (18, 40, frozenset({1}))     # the old test is left out
    assert packages[11].test_ids == frozenset()


def test_test_names_are_found_by_their_english_or_arabic_name_without_case_or_extra_spaces(db):
    seed.add_test(db, 1, "HbA1c ", name_ar="الهيموجلوبين السكري")
    seed.add_test(db, 2, "Vitamin  D")
    seed.add_test(db, 3, "Old", active=False)
    seed.add_test(db, 4, "Vitamin D")                        # the same name as test 2 once spaces are collapsed
    catalogue = PostgresCatalogue(db)
    by_name = catalogue.test_ids_by_name()
    assert by_name == {"hba1c": 1, "الهيموجلوبين السكري": 1, "vitamin d": 2}   # the lower id keeps a shared name
    assert catalogue.test_names()[1] == ("HbA1c ", "الهيموجلوبين السكري")
    assert catalogue.test_names()[3] == ("Old", None)                           # names are for every test


def test_the_package_data_is_kept_for_a_while_so_a_batch_does_not_ask_the_database_for_every_visit(db):
    now = [0.0]
    seed.add_test(db, 1, "Urea")
    _add_package(db, 10, "First", tests=(1,))
    catalogue = PostgresCatalogue(db, cache_s=300, clock=lambda: now[0])
    assert [p.name for p in catalogue.packages()] == ["First"]
    _add_package(db, 11, "Second", tests=(1,))
    now[0] = 299
    assert [p.name for p in catalogue.packages()] == ["First"]                  # still the saved copy
    now[0] = 300
    assert [p.name for p in catalogue.packages()] == ["First", "Second"]        # read again


def test_without_a_cache_every_call_reads_the_database(db):
    catalogue = PostgresCatalogue(db, cache_s=0)
    assert catalogue.packages() == []
    _add_package(db, 10, "New")
    assert [p.name for p in catalogue.packages()] == ["New"]
