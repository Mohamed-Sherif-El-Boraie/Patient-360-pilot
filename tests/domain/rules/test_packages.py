from patient360.domain.rules.packages import Package, package_entries


def test_each_entry_has_the_package_name_and_its_tests():
    package = Package(5, "Routine check-up", 2, 18, 40, frozenset({3, 1}))
    assert package_entries([package], {1: "Urea", 3: "Glucose"}) == [
        {"package_name": "Routine check-up", "tests": [{"test_name": "Urea"}, {"test_name": "Glucose"}]}]


def test_the_order_of_the_packages_is_kept():
    first = Package(1, "A", 3, None, None, frozenset())
    second = Package(2, "B", 3, None, None, frozenset())
    assert [e["package_name"] for e in package_entries([second, first], {})] == ["B", "A"]


def test_a_test_without_a_known_name_shows_its_id_and_no_packages_gives_an_empty_list():
    unknown = Package(1, "A", 3, None, None, frozenset({9}))
    assert package_entries([unknown], {})[0]["tests"] == [{"test_name": "9"}]
    assert package_entries([], {}) == []


def test_a_package_holds_sex_age_group_and_tests():
    package = Package(5, "Routine check-up", 2, 18, 40, frozenset({1, 2, 3}))
    assert (package.sex_id, package.age_min, package.age_max) == (2, 18, 40)
    assert Package(6, "Any age and sex", 3, None, None, frozenset()).sex_id == 3
