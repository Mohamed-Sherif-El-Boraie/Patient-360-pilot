from datetime import timedelta

import pytest

from patient360.domain.rules.dispatch import SOURCE_PRIORITY, delivery_retry_after, retry_after


def test_urgent_then_symptom_checker_then_on_demand_then_batch():
    assert sorted(SOURCE_PRIORITY, key=SOURCE_PRIORITY.get) == ["urgent", "symptom-checker", "on-demand", "batch"]


def test_four_quick_retries_then_ten_every_15_minutes_and_the_15th_failure_gives_up():
    assert [retry_after(n) for n in (1, 2, 3, 4)] == [timedelta(seconds=s) for s in (10, 30, 120, 600)]
    assert [retry_after(n) for n in range(5, 15)] == [timedelta(minutes=15)] * 10
    assert retry_after(15) is None and retry_after(50) is None


def test_a_delivery_to_the_wrapper_never_gives_up():
    assert [delivery_retry_after(n) for n in (1, 2, 3, 4)] == [timedelta(seconds=s) for s in (10, 30, 120, 600)]
    assert {delivery_retry_after(n) for n in (5, 6, 100, 10_000)} == {timedelta(minutes=15)}


def test_failures_are_counted_from_one():
    with pytest.raises(ValueError):
        retry_after(0)
    with pytest.raises(ValueError):
        delivery_retry_after(0)
