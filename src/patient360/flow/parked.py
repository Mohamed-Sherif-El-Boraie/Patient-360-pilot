"""Saving the payloads that were parked in Blob (``_pending``) because a step of ours failed while receiving them.

The sender was told "accepted" (see ``Intake.receive``), so these must end up saved. Each one goes through the normal
intake again, which is safe to repeat: a visit saved in the meantime is only a duplicate and its file is removed.
"""
from __future__ import annotations

from dataclasses import dataclass

import structlog

from patient360.domain.common.blob_paths import PENDING_PREFIX
from patient360.failure import StepFailed, error_name, where
from patient360.flow.intake import Intake
from patient360.ports import BlobStore, BlobUnavailable

log = structlog.get_logger()

MAX_PER_RUN = 200       # a long outage leaves many files: they are saved over several runs, not in one long one


@dataclass(frozen=True)
class Recovered:
    path: str
    outcome: str        # the name of the intake outcome: Accepted, Retry, Duplicate, ...


class ParkedRecovery:
    def __init__(self, intake: Intake, blobs: BlobStore) -> None:
        self._intake, self._blobs = intake, blobs

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
                log.warning("parked_not_saved_yet", path=path, step=failed.step, visit_id=failed.visit_id,
                            error_type=error_name(failed.error), where=where(failed.error))
                continue
            except Exception as error:
                log.warning("parked_not_saved_yet", path=path, step="recovering a parked payload",
                            error_type=error_name(error), where=where(error))
                continue
            log.info("parked_saved", path=path, outcome=type(outcome).__name__)
            done.append(Recovered(path, type(outcome).__name__))
        return done
