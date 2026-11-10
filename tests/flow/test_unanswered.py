from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from patient360 import timers
from patient360.flow.unanswered import UnansweredSweep
from tests.fakes import FakeBlobs, FakeCatalogue, FakeS3, FakeStore
from tests.test_timers import make_services

T0 = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)


def test_the_check_asks_the_database_at_most_once_an_hour_and_counts_5_hours_back_from_now():
    asked = []
    store = SimpleNamespace(visits_without_answer=lambda sent_before: asked.append(sent_before) or [])
    sweep = UnansweredSweep(store, None, 5 * 3600, 3600)
    for minutes in (0, 5, 55, 59, 60, 65, 125):
        sweep.run(T0 + timedelta(minutes=minutes))
    assert asked == [T0 - timedelta(hours=5), T0 + timedelta(hours=1) - timedelta(hours=5),
                     T0 + timedelta(minutes=125) - timedelta(hours=5)]


def test_the_job_cycle_runs_the_unanswered_check_with_the_time_of_the_cycle():
    runs = []
    spy = SimpleNamespace(run=runs.append)
    services = replace(make_services(FakeStore(), FakeBlobs(), FakeS3(), FakeCatalogue()), unanswered=spy)
    timers.run_job_cycle(services, T0)
    assert runs == [T0]
