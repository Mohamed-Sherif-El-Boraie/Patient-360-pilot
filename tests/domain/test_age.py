from datetime import date

from patient360.domain.age import age_in_days

TODAY = date(2026, 10, 23)


def test_a_baby_is_counted_in_days_not_years():
    assert age_in_days("2026-09-25", TODAY) == 28
    assert age_in_days("2026-10-23", TODAY) == 0


def test_an_adult_in_days():
    assert age_in_days("1986-10-23", TODAY) == 14610


def test_a_birthdate_in_the_future_is_zero_not_negative():
    assert age_in_days("2027-01-01", TODAY) == 0


def test_without_a_date_it_counts_to_today():
    assert age_in_days(date.today().isoformat()) == 0
