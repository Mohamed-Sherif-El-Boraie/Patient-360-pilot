from datetime import timedelta

import pytest

from patient360.domain.rules.dispatch import SOURCE_PRIORITY, retry_after


def test_urgent_then_symptom_checker_then_on_demand_then_batch():
    assert sorted(SOURCE_PRIORITY, key=SOURCE_PRIORITY.get) == ["urgent", "symptom-checker", "on-demand", "batch"]


def test_the_wait_grows_and_the_fifth_failure_gives_up():
    assert [retry_after(n) for n in (1, 2, 3, 4)] == [timedelta(seconds=s) for s in (10, 30, 120, 600)]
    assert retry_after(5) is None and retry_after(50) is None


def test_failures_are_counted_from_one():
    with pytest.raises(ValueError):
        retry_after(0)
