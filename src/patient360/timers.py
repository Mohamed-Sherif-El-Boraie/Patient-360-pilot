"""The two background loops of the running service.

The job loop (every ``job_interval_s``, 5 minutes) picks up what a restart cut short and starts the batches. The work
loop (every ``work_interval_s``, 5 seconds) uploads to S3 and delivers to the Backend. A cycle is plain synchronous code
so tests can call it; one failing job is logged and never stops the next.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import structlog

from patient360.flow.process import process_stuck_visits
from patient360.flow.symptom import process_waiting_symptoms
from patient360.services import Services

log = structlog.get_logger()


def _run_each(jobs: list[Callable[[], Any]]) -> None:
    for job in jobs:
        try:
            job()
        except Exception as error:      # a job may fail in any way; the others must still run
            log.warning("job_failed", error_type=type(error).__name__)


def run_job_cycle(services: Services, now: datetime) -> None:
    _run_each([
        lambda: process_stuck_visits(services.store, services.processor, now),
        lambda: process_waiting_symptoms(services.store, services.symptom_processor, now),
        services.batch_trigger.run,
    ])


def run_work_cycle(services: Services, now: datetime) -> None:
    jobs: list[Callable[[], Any]] = [services.dispatcher.run]
    if services.sender is not None:
        jobs.append(services.sender.run)
    _run_each(jobs)


async def _loop(cycle: Callable[[Services, datetime], None], services: Services, interval_s: float) -> None:
    """Run ``cycle`` in a thread, wait ``interval_s``, again: the next cycle starts after the previous one finished."""
    while True:
        await asyncio.to_thread(cycle, services, datetime.now(UTC))
        await asyncio.sleep(interval_s)


def lifespan(services: Services):
    """For ``FastAPI(lifespan=...)``: starts both loops, and on shutdown stops them and closes the database."""

    @asynccontextmanager
    async def manager(app: Any = None) -> AsyncIterator[None]:
        loops = [asyncio.create_task(_loop(run_job_cycle, services, services.settings.job_interval_s)),
                 asyncio.create_task(_loop(run_work_cycle, services, services.settings.work_interval_s))]
        try:
            yield
        finally:
            for task in loops:
                task.cancel()
            for task in loops:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            services.db.close()

    return manager
