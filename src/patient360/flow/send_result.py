"""Sending each finished result to the Backend: one JSON per call, retried when the Backend is busy or unreachable."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import structlog

from patient360.domain.rules.dispatch import retry_after
from patient360.ports import BackendClient, BackendRejected, BackendUnavailable, DeliveryUnit, VisitStore

log = structlog.get_logger()


@dataclass(frozen=True)
class Delivered:
    unit: DeliveryUnit
    outcome: Literal["delivered", "retry", "failed"]


class ResultSender:
    def __init__(self, store: VisitStore, backend: BackendClient, now: Callable[[], datetime]) -> None:
        self._store, self._backend, self._now = store, backend, now

    def run(self) -> list[Delivered]:
        """Send every result that is ready. A result that failed waits for its retry time, so the loop always ends."""
        done = []
        while (unit := self._store.claim_next_delivery(self._now())) is not None:
            done.append(self._send(unit))
        return done

    def _send(self, unit: DeliveryUnit) -> Delivered:
        try:
            self._backend.post_result(self._store.public_output_of(unit))
        except BackendUnavailable as error:
            return self._fail(unit, error, retry_at=self._retry_time(unit))
        except BackendRejected as error:      # the same body would be refused again: do not waste tries
            return self._fail(unit, error, retry_at=None)
        self._store.mark_delivered(unit)
        log.info("delivered", source=unit.source, attempt=unit.attempts + 1)
        return Delivered(unit, "delivered")

    def _retry_time(self, unit: DeliveryUnit) -> datetime | None:
        delay = retry_after(unit.attempts + 1)
        return None if delay is None else self._now() + delay

    def _fail(self, unit: DeliveryUnit, error: Exception, retry_at: datetime | None) -> Delivered:
        reason = str(error) or type(error).__name__      # the adapter's readable message, saved as last_error
        self._store.mark_delivery_failed_attempt(unit, reason, retry_at)
        outcome = "retry" if retry_at is not None else "failed"
        log.warning("delivery_failed", source=unit.source, attempt=unit.attempts + 1, reason=reason, status=outcome)
        return Delivered(unit, outcome)
