import pytest

from patient360.domain.packages import Package, PackageMatch, add_packages_to_output, fill_down_package_rows


def match(package_id=7, name="Kidney", tests=(1, 2), *, category=None, score=None):
    return PackageMatch(1, package_id, name, category, tests, score)


def test_attach_packages_adds_the_list_under_ai():
    out = add_packages_to_output({"ai": {"summary": {"en": "x"}}}, [match()], {1: "Estimated (eGFR)", 2: "Uric acid"})
    assert out["ai"]["summary"] == {"en": "x"}
    assert out["ai"]["packages"] == [
        {"rank": 1, "package_id": 7, "name": "Kidney", "matched_tests": ["Estimated (eGFR)", "Uric acid"]}]


def test_attach_packages_keeps_category_and_score_only_when_set():
    out = add_packages_to_output({}, [match(category="ROUTINE", score=0.5)], {1: "A", 2: "B"})
    assert out["ai"]["packages"][0]["category"] == "ROUTINE" and out["ai"]["packages"][0]["score"] == 0.5


def test_no_matches_gives_an_empty_list_and_creates_ai_if_missing():
    assert add_packages_to_output({}, [], {}) == {"ai": {"packages": []}}


def test_attach_packages_does_not_mutate_its_input():
    source = {"ai": {}}
    add_packages_to_output(source, [match()], {})
    assert source == {"ai": {}}


def test_attach_packages_survives_a_test_without_a_known_name():
    out = add_packages_to_output({}, [match()], {1: "A"})
    assert out["ai"]["packages"][0]["matched_tests"] == ["A", "2"]


def test_forward_fill_keeps_a_package_without_tests():
    rows = [
        (90, "Post-Bariatric  Surgery", 612, "Serum Iron"),
        (None, None, 87, "Total HCG (Quantitative)"),
        (6283, "Empty package", None, None),
        (91, "Tumor Marker screening", 5, "AFP"),
    ]
    packages, items = fill_down_package_rows(rows)
    assert packages == {90: "Post-Bariatric Surgery", 6283: "Empty package", 91: "Tumor Marker screening"}
    assert items == [(90, 612, "Serum Iron"), (90, 87, "Total HCG (Quantitative)"), (91, 5, "AFP")]


def test_a_workbook_row_with_a_name_but_no_id_is_skipped_and_a_blank_name_is_empty_text():
    packages, items = fill_down_package_rows([(1, "P", None, "orphan name"), (None, None, 7, None)])
    assert items == [(1, 7, "")]


def test_forward_fill_rejects_a_test_row_before_any_package():
    with pytest.raises(ValueError):
        fill_down_package_rows([(None, None, 1, "x")])


def test_a_package_holds_category_gender_age_group_and_tests():
    package = Package(5, "Routine check-up", "ROUTINE", "female", 18, 40, frozenset({1, 2, 3}))
    assert (package.category, package.gender, package.age_min, package.age_max) == ("ROUTINE", "female", 18, 40)
    everyone = Package(6, "Any age and gender", "NON-ROUTINE", "any", None, None, frozenset())
    assert everyone.gender == "any" and everyone.age_min is None
