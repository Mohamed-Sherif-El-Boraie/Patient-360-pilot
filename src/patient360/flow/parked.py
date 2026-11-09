"""Saving the payloads that were parked in Blob (``_pending``) because a step of ours failed while receiving them.

The sender was told "accepted" (see ``Intake.receive``), so these must end up saved. Each one goes through the normal
intake again, which is safe to repeat: a visit saved in the meantime is only a duplicate and its file is removed.

A file that cannot be saved is told once as a warning, and as an ERROR (``parked_stuck``) once it has waited 15 minutes,
and again every 15 minutes, so an alert on that line finds a visit nobody is working on.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import structlog

from patient360.domain.common.blob_paths import PENDING_PREFIX
from patient360.failure import StepFailed, error_name, where
from patient360.flow.intake import Intake
from patient360.ports import BlobStore, BlobUnavailable

log = structlog.get_logger()

MAX_PER_RUN = 200       # a long outage leaves many files: they are saved over several runs, not in one long one
ALERT_AFTER_S = 15 * 60


@dataclass(frozen=True)
class Recovered:
    path: str
    outcome: str        # the name of the intake outcome: Accepted, Retry, Duplicate, ...


class ParkedRecovery:
    def __init__(self, intake: Intake, blobs: BlobStore, clock: Callable[[], float] = time.monotonic) -> None:
        self._intake, self._blobs, self._clock = intake, blobs, clock
        self._first_seen: dict[str, float] = {}      # path -> when this instance first failed to save it
        self._last_alert: dict[str, float] = {}      # path -> when it was last told as an error

    def run(self) -> list[Recovered]:
        """Save every parked payload. One that still fails stays for the next run; a file is removed only after its
        visit is saved (or was already)."""
        try:
            paths = self._blobs.list(PENDING_PREFIX)[:MAX_PER_RUN]
        except BlobUnavailable:
            return []
        done = []
        for path in paths:
            try:
                raw = self._blobs.get(path)
                if raw is None:
                    continue
                outcome = self._intake.receive(raw, park=False)
                self._blobs.delete(path)
            except StepFailed as failed:        # the database is still down: the file stays
                self._not_saved(path, failed.step, failed.visit_id, failed.error)
                continue
            except Exception as error:
                self._not_saved(path, "recovering a parked payload", None, error)
                continue
            self._first_seen.pop(path, None)
            self._last_alert.pop(path, None)
            log.info("parked_saved", path=path, outcome=type(outcome).__name__)
            done.append(Recovered(path, type(outcome).__name__))
        return done

    def _not_saved(self, path: str, step: str, visit_id: int | None, error: BaseException) -> None:
        now = self._clock()
        first = self._first_seen.setdefault(path, now)
        fields = {"path": path, "step": step, "visit_id": visit_id, "error_type": error_name(error),
                  "where": where(error)}
        if first == now:
            log.warning("parked_not_saved_yet", **fields)
        elif now - first >= ALERT_AFTER_S and now - self._last_alert.get(path, first) >= ALERT_AFTER_S:
            self._last_alert[path] = now
            log.error("parked_stuck", minutes_waiting=int((now - first) // 60), **fields)
