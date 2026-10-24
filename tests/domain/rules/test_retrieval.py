from patient360.domain.rules.retrieval import (
    CatalogueParameter,
    CatalogueTest,
    build_catalogue_maps,
    select_retrieved_tests,
)

KIDNEY, SUGAR = 1, 2                      # category ids
CATALOGUE = {
    KIDNEY: [CatalogueTest(1, "Estimated (eGFR)", "kidney filter"),
             CatalogueTest(2, "Urea", "kidney urea"),
             CatalogueTest(3, "Cystatin C", None)],
    SUGAR: [CatalogueTest(4, "HbA1c", "average sugar")],
}
CATEGORIES = {10: [KIDNEY], 11: [SUGAR], 12: []}      # parameter id -> its category ids


def row(parameter_id, status, value="1"):
    return {"parameter_id": parameter_id, "status": status, "value": value}


def run(rows, performed=()):
    return select_retrieved_tests(rows, CATEGORIES, CATALOGUE, performed)


def test_all_normal_parameters_retrieve_nothing():
    assert run([row(10, "normal"), row(11, "normal")]) == []


def test_a_not_normal_parameter_pulls_every_test_of_its_category():
    names = [t["test_name"] for t in run([row(10, "high")])]
    assert names == ["Estimated (eGFR)", "Urea", "Cystatin C"]


def test_tests_the_patient_already_did_are_removed_ignoring_case_and_spaces():
    names = [t["test_name"] for t in run([row(10, "high")], performed=["urea", "  Estimated   (eGFR) "])]
    assert names == ["Cystatin C"]


def test_the_category_of_a_critical_parameter_comes_first_then_the_others():
    names = [t["test_name"] for t in run([row(10, "low"), row(10, "text"), row(11, "critical")])]
    assert names == ["HbA1c", "Estimated (eGFR)", "Urea", "Cystatin C"]
    names = [t["test_name"] for t in run([row(11, "high"), row(10, "critical")])]
    assert names == ["Estimated (eGFR)", "Urea", "Cystatin C", "HbA1c"]


def test_at_most_fifty_tests_are_returned_by_default_and_the_limit_can_be_changed():
    many = {7: [CatalogueTest(i, f"Test {i}", None) for i in range(1, 121)]}
    rows = [{"parameter_id": 1, "status": "high", "value": "1"}]
    assert len(select_retrieved_tests(rows, {1: [7]}, many, [])) == 50
    assert len(select_retrieved_tests(rows, {1: [7]}, many, [], max_tests=7)) == 7


def test_the_cap_keeps_the_critical_categorys_tests_first():
    many = {5: [CatalogueTest(i, f"C{i}", None) for i in range(1, 40)],
            6: [CatalogueTest(100 + i, f"O{i}", None) for i in range(1, 40)]}
    rows = [{"parameter_id": 1, "status": "critical", "value": "1"},
            {"parameter_id": 2, "status": "high", "value": "1"}]
    names = [t["test_name"] for t in select_retrieved_tests(rows, {1: [5], 2: [6]}, many, [])]
    assert len(names) == 50 and names[:39] == [f"C{i}" for i in range(1, 40)] and names[39] == "O1"


def test_each_entry_has_the_name_and_the_description():
    first = run([row(11, "high")])[0]
    assert first == {"test_name": "HbA1c", "test_description": "average sugar"}


def test_a_parameter_without_a_category_or_a_value_adds_nothing():
    assert run([row(12, "high")]) == []
    assert run([row(10, "text", value="")]) == []


def test_include_normal_also_takes_the_categories_of_normal_parameters_after_the_others():
    rows = [row(11, "normal"), row(10, "high")]
    names = [t["test_name"] for t in select_retrieved_tests(rows, CATEGORIES, CATALOGUE, (), include_normal=True)]
    assert names == ["Estimated (eGFR)", "Urea", "Cystatin C", "HbA1c"]        # not-normal category first
    all_normal = [row(11, "normal"), row(10, "normal")]
    names = [t["test_name"] for t in select_retrieved_tests(all_normal, CATEGORIES, CATALOGUE, (), include_normal=True)]
    assert names == ["Estimated (eGFR)", "Urea", "Cystatin C", "HbA1c"]
    assert select_retrieved_tests(all_normal, CATEGORIES, CATALOGUE, ()) == []   # off by default


def test_a_parameter_in_two_categories_pulls_both_and_a_test_in_two_categories_comes_once():
    shared = CatalogueTest(9, "Shared test", None)
    catalogue = {KIDNEY: [*CATALOGUE[KIDNEY], shared], SUGAR: [*CATALOGUE[SUGAR], shared]}
    names = [t["test_name"] for t in select_retrieved_tests(
        [row(20, "high")], {20: [KIDNEY, SUGAR]}, catalogue, ())]
    assert names == ["Estimated (eGFR)", "Urea", "Cystatin C", "Shared test", "HbA1c"]


def names_for(rows, tests, parameters, active_categories=(1, 2)):
    by_parameter, by_category = build_catalogue_maps(active_categories, tests, parameters)
    return [t["test_name"] for t in select_retrieved_tests(rows, by_parameter, by_category, ())]


def test_an_inactive_test_is_never_recommended():
    tests = [CatalogueTest(1, "Old test", None, (1,), active=False), CatalogueTest(2, "New test", None, (1,))]
    assert names_for([row(10, "high")], tests, [CatalogueParameter(10, (1,))]) == ["New test"]


def test_an_inactive_category_is_ignored_but_a_test_in_an_active_one_is_still_found():
    tests = [CatalogueTest(1, "Only old", None, (3,)), CatalogueTest(2, "Both", None, (3, 1))]
    parameters = [CatalogueParameter(10, (3,)), CatalogueParameter(11, (3, 1))]
    assert names_for([row(10, "high")], tests, parameters, active_categories=(1,)) == []
    assert names_for([row(11, "high")], tests, parameters, active_categories=(1,)) == ["Both"]


def test_an_inactive_parameter_picks_no_tests_but_is_still_a_not_normal_row():
    tests = [CatalogueTest(1, "Kidney test", None, (1,))]
    parameters = [CatalogueParameter(10, (1,), active=False)]
    assert names_for([row(10, "critical")], tests, parameters) == []


def test_an_unknown_category_id_is_ignored_and_a_repeated_one_gives_the_test_once():
    tests = [CatalogueTest(1, "Test", None, (1, 1, 99))]
    assert names_for([row(10, "high")], tests, [CatalogueParameter(10, (1, 99))]) == ["Test"]


def test_the_maps_keep_only_active_things():
    by_parameter, by_category = build_catalogue_maps(
        (2,), [CatalogueTest(1, "A", None, (1, 2)), CatalogueTest(2, "B", None, (2,), active=False)],
        [CatalogueParameter(10, (1, 2)), CatalogueParameter(11, (1,), active=False)])
    assert by_parameter == {10: (2,)}
    assert {category: [t.test_id for t in tests] for category, tests in by_category.items()} == {2: [1]}

