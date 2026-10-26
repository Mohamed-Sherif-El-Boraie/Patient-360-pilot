import pytest

from patient360.domain.rules.packages import MAX_PACKAGES, Package, normalize_name, package_entries, select_packages

YEAR = 365.25


def package(package_id, tests=(), *, sex=3, age=(None, None), kind="GENERAL", name=None, name_ar=None):
    return Package(package_id, name or f"P{package_id}", sex, age[0], age[1], frozenset(tests), name_ar, kind)


def ids(chosen):
    return [p.package_id for p in chosen]


def adult(*packages_and_tests, age_years=30, sex=1, wanted=()):
    return ids(select_packages(list(packages_and_tests), wanted, age_years * YEAR, sex))


# ---- who a package fits ---------------------------------------------------------------------------------------------
def test_a_package_must_fit_the_sex_and_the_age_in_completed_years():
    men, women, both = package(1, {1}, sex=1), package(2, {1}, sex=2), package(3, {1}, sex=3)
    assert adult(men, women, both, wanted={1}, sex=1)[:1] == [1] and 2 not in adult(men, women, both, wanted={1}, sex=1)
    child = package(4, {1}, age=(0, 5))
    assert select_packages([child], {1}, 5.9 * YEAR, 1) == [child]              # 5.9 years is 5 completed years
    assert select_packages([child], {1}, 6 * YEAR, 1) == []                      # 6 years: the package ends at 5
    assert select_packages([child], {1}, 0, 1) == [child]                        # a newborn


def test_no_age_limits_means_any_age():
    anyone = package(1, {1})
    assert select_packages([anyone], {1}, 99 * YEAR, 2) == [anyone]


# ---- the tests the AI asked for -------------------------------------------------------------------------------------
def test_the_package_that_covers_more_of_the_requested_tests_wins():
    small, big = package(1, {1}), package(2, {1, 2})
    assert adult(small, big, wanted={1, 2}) == [2]


def test_focus_breaks_a_coverage_tie_a_package_with_less_unrelated_tests_wins():
    focused, bloated = package(1, {1, 2}), package(2, {1, 2, 7, 8, 9, 10})
    assert adult(bloated, focused, wanted={1, 2}) == [1]


def test_a_package_that_repeats_tests_already_covered_is_skipped_but_one_with_new_tests_is_kept():
    first, repeat, other = package(1, {1, 2}), package(2, {1, 2}), package(3, {3})
    assert adult(first, repeat, other, wanted={1, 2, 3}) == [1]          # 3 scores too low to be near-best
    near = [package(1, {1, 2, 3}), package(2, {1, 2, 3, 4}), package(3, {1, 9})]
    assert adult(*near, wanted={1, 2, 3}) == [1]                          # overlapping packages are not stacked


def test_near_best_packages_on_different_tests_are_both_returned():
    # each covers half of the need (coverage 0.5) and all of its own size (focus 1.0): equal scores, no overlap
    left, right = package(1, {1, 2}), package(2, {3, 4})
    assert adult(right, left, wanted={1, 2, 3, 4}) == [1, 2]              # equal score: the lower id first


def test_ties_are_broken_the_same_way_every_time():
    twin_a, twin_b = package(8, {1}), package(9, {1})
    assert adult(twin_b, twin_a, wanted={1}) == [8] and adult(twin_a, twin_b, wanted={1}) == [8]


# ---- routine packages ----------------------------------------------------------------------------------------------
def test_when_nothing_matched_the_routine_packages_are_returned_in_id_order_with_numbers_sorted_as_numbers():
    routine = [package(10, {1}, kind="WELLNESS"), package(9, {2}, kind="WELLNESS"), package(100, {3}, kind="WELLNESS")]
    assert adult(*routine, package(5, {7}), wanted=()) == [9, 10, 100]
    assert adult(*routine, wanted={99}) == [9, 10, 100]                    # a test that is in no package


def test_from_fifty_the_routine_packages_are_added_after_the_matching_ones():
    matching, routine = package(1, {1}), package(2, {5}, kind="WELLNESS")
    assert adult(matching, routine, wanted={1}, age_years=49) == [1]
    assert adult(matching, routine, wanted={1}, age_years=50) == [1, 2]
    assert adult(matching, routine, wanted={1}, age_years=71) == [1, 2]


def test_a_routine_package_that_already_matched_is_not_added_twice():
    both = package(1, {1}, kind="WELLNESS")
    assert adult(both, wanted={1}, age_years=60) == [1]


def test_a_routine_package_must_fit_the_patient_too():
    young_only = package(1, {1}, kind="WELLNESS", age=(0, 40))
    assert adult(young_only, wanted=(), age_years=60) == []


def test_a_fully_normal_patient_over_fifty_gets_the_routine_packages():
    routine = [package(i, {i}, kind="WELLNESS", age=(50, 150)) for i in (1, 2)]
    assert adult(*routine, wanted=(), age_years=62) == [1, 2]


def test_never_more_than_the_maximum():
    many = [package(i, {100 + i}, kind="WELLNESS") for i in range(1, 12)]
    assert len(adult(*many, wanted=(), age_years=70)) == MAX_PACKAGES == 5
    assert adult(*many, wanted=(), age_years=70) == [1, 2, 3, 4, 5]
    covering = [package(i, {i}) for i in range(1, 12)]
    assert len(adult(*covering, *many, wanted=set(range(1, 12)), age_years=70)) == MAX_PACKAGES


def test_no_packages_at_all_gives_nothing():
    assert select_packages([], {1}, 30 * YEAR, 1) == []


@pytest.mark.parametrize("text, expected", [("HbA1c ", "hba1c"), ("  Vitamin   D\t(25-OH)", "vitamin d (25-oh)"),
                                           ("", "")])
def test_names_are_matched_without_case_and_extra_spaces(text, expected):
    assert normalize_name(text) == expected


# ---- the entries in the result ---------------------------------------------------------------------------------------
def test_each_entry_has_english_and_arabic_names_of_the_package_and_of_its_tests():
    entry = package(5, {3, 1}, name="Routine check-up", name_ar="الفحص الدوري")
    assert package_entries([entry], {1: ("Urea", "اليوريا"), 3: ("Glucose", "الجلوكوز")}) == [{
        "package_name": {"en": "Routine check-up", "ar": "الفحص الدوري"},
        "tests": [{"test_name": {"en": "Urea", "ar": "اليوريا"}}, {"test_name": {"en": "Glucose", "ar": "الجلوكوز"}}]}]


def test_a_missing_arabic_name_shows_the_english_one_and_an_unknown_test_shows_its_id():
    entry = package(1, {9, 2}, name="A")
    out = package_entries([entry], {2: ("Urea", None)})
    assert out[0]["package_name"] == {"en": "A", "ar": "A"}
    assert out[0]["tests"] == [{"test_name": {"en": "Urea", "ar": "Urea"}}, {"test_name": {"en": "9", "ar": "9"}}]


def test_the_order_of_the_packages_is_kept_and_no_packages_gives_an_empty_list():
    first, second = package(1, name="A"), package(2, name="B")
    assert [e["package_name"]["en"] for e in package_entries([second, first], {})] == ["B", "A"]
    assert package_entries([], {}) == []
