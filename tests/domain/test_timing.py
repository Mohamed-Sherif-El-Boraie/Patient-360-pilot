from datetime import UTC, datetime

import pytest

from patient360.domain.timing import PHASES, PhaseTimer


class FakeClock:
    def __init__(self, *moments: float) -> None:
        self._moments = list(moments)

    def __call__(self) -> float:
        return self._moments.pop(0)


NOW = datetime(2026, 10, 19, 8, 0, tzinfo=UTC)


def timer(*moments: float) -> PhaseTimer:
    return PhaseTimer(clock=FakeClock(*moments), now=lambda: NOW)


def test_each_measured_block_becomes_one_timing_in_milliseconds():
    t = timer(10.0, 10.25, 20.0, 23.5)
    with t.measure("process"):
        pass
    with t.measure("aws_round_trip"):
        pass
    assert [(x.phase, x.duration_ms) for x in t.timings] == [("process", 250), ("aws_round_trip", 3500)]
    assert t.timings[0].started_at == NOW


def test_a_block_that_raises_is_still_timed_and_the_error_continues():
    t = timer(1.0, 1.5)
    with pytest.raises(RuntimeError), t.measure("deliver"):
        raise RuntimeError("backend down")
    assert [(x.phase, x.duration_ms) for x in t.timings] == [("deliver", 500)]


def test_an_unknown_phase_name_is_a_programming_error():
    with pytest.raises(ValueError), timer().measure("proccess"):
        pass


def test_the_real_clock_gives_a_non_negative_duration():
    t = PhaseTimer()
    with t.measure("validate"):
        pass
    assert t.timings[0].duration_ms >= 0 and t.timings[0].started_at.tzinfo is not None


def test_the_phases_are_the_ones_the_owner_asked_to_trace():
    assert PHASES == ("validate", "process", "queue_wait", "aws_round_trip", "post_process", "deliver")
