from patient360.domain.rules.retrieval import CatalogueTest, select_retrieved_tests

CATALOGUE = {
    "Kidney": [CatalogueTest(1, "Estimated (eGFR)", "kidney filter", "Kidney"),
               CatalogueTest(2, "Urea", "kidney urea", "Kidney"),
               CatalogueTest(3, "Cystatin C", None, "Kidney")],
    "Sugar": [CatalogueTest(4, "HbA1c", "average sugar", "Sugar")],
}
CATEGORIES = {10: "Kidney", 11: "Sugar", 12: None}


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
    many = {"Big": [CatalogueTest(i, f"Test {i}", None, "Big") for i in range(1, 121)]}
    rows = [{"parameter_id": 1, "status": "high", "value": "1"}]
    assert len(select_retrieved_tests(rows, {1: "Big"}, many, [])) == 50
    assert len(select_retrieved_tests(rows, {1: "Big"}, many, [], max_tests=7)) == 7


def test_the_cap_keeps_the_critical_categorys_tests_first():
    many = {"Crit": [CatalogueTest(i, f"C{i}", None, "Crit") for i in range(1, 40)],
            "Other": [CatalogueTest(100 + i, f"O{i}", None, "Other") for i in range(1, 40)]}
    rows = [{"parameter_id": 1, "status": "critical", "value": "1"},
            {"parameter_id": 2, "status": "high", "value": "1"}]
    names = [t["test_name"] for t in select_retrieved_tests(rows, {1: "Crit", 2: "Other"}, many, [])]
    assert len(names) == 50 and names[:39] == [f"C{i}" for i in range(1, 40)] and names[39] == "O1"


def test_each_entry_has_the_name_and_the_description():
    first = run([row(11, "high")])[0]
    assert first == {"test_name": "HbA1c", "test_description": "average sugar"}


def test_a_parameter_without_a_category_or_a_value_adds_nothing():
    assert run([row(12, "high")]) == []
    assert run([row(10, "text", value="")]) == []
