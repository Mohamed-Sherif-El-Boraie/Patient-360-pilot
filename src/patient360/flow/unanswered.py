"""Visits AWS never answered: after ``AWS_ANSWER_TIMEOUT_S`` (5 hours) the Backend is told the visit failed.

The same message as for an AWS failure (``success`` false, the critical tests of the visit). A visit of a batch is
failed the same way, one by one, for each visit of the batch that has no answer. An answer that still comes later is
kept in the ``quarantine`` folder of the output container and never sent (see ``OutputReceiver``). Nothing is sent to
AWS again.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import structlog

from patient360.failure import error_name, where
from patient360.flow.results import OutputReceiver
from patient360.ports import Unanswered, VisitStore

log = structlog.get_logger()


class UnansweredSweep:
    """``fail_unanswered`` at most once per ``every_s`` (an hour), though the job loop wakes every 5 minutes: the other
    jobs of that loop need the short interval, this one only reads the database. A failure goes out up to ``every_s``
    after the timeout."""

    def __init__(self, store: VisitStore, receiver: OutputReceiver, timeout_s: float, every_s: float) -> None:
        self._store, self._receiver, self._timeout_s, self._every_s = store, receiver, timeout_s, every_s
        self._last: datetime | None = None

    def run(self, now: datetime) -> list[Unanswered]:
        if self._last is not None and now - self._last < timedelta(seconds=self._every_s):
            return []
        self._last = now
        return fail_unanswered(self._store, self._receiver, now, self._timeout_s)


def fail_unanswered(store: VisitStore, receiver: OutputReceiver, now: datetime, timeout_s: float) -> list[Unanswered]:
    """One sweep. A visit that cannot be failed now (the database is busy) stays and is tried in the next sweep."""
    done = []
    for item in store.visits_without_answer(now - timedelta(seconds=timeout_s)):
        try:
            receiver.fail_unanswered(item, now)
        except Exception as error:
            log.warning("no_answer_not_failed", step="telling the Backend that AWS did not answer",
                        visit_id=item.visit_id, error_type=error_name(error), where=where(error))
            continue
        done.append(item)
    return done
