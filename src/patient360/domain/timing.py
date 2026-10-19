"""Wall-clock time of each pipeline phase. One ``PhaseTimer`` per record and attempt; the rows go to
``record_phase_timings`` so slow phases can be traced later."""
from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

# validate: check the payload | process: build the AI document, tier and lane
# aws_round_trip: sent to AWS until the answer is back | post_process: packages, html, strip internal fields
# deliver: POST to the Backend
PHASES = ("validate", "process", "aws_round_trip", "post_process", "deliver")


@dataclass(frozen=True)
class PhaseTiming:
    phase: str
    started_at: datetime
    duration_ms: int


def _utc_now() -> datetime:
    return datetime.now(UTC)


class PhaseTimer:
    def __init__(
        self, *, clock: Callable[[], float] = time.perf_counter, now: Callable[[], datetime] = _utc_now
    ) -> None:
        self._clock = clock
        self._now = now
        self.timings: list[PhaseTiming] = []

    @contextmanager
    def measure(self, phase: str) -> Iterator[None]:
        """``with timer.measure("process"): ...``  A block that raises is still timed, then the error continues."""
        if phase not in PHASES:
            raise ValueError(f"unknown phase {phase!r}; use one of {PHASES}")
        started_at, started = self._now(), self._clock()
        try:
            yield
        finally:
            self.timings.append(PhaseTiming(phase, started_at, round((self._clock() - started) * 1000)))
