"""Dispatch rules: which source goes to AWS first, and how long to wait before the next try. Pure."""
from __future__ import annotations

from datetime import timedelta

from patient360.domain.common.compat import frozendict

# Smaller number = sent first.
SOURCE_PRIORITY = frozendict({"urgent": 0, "symptom-checker": 1, "on-demand": 2, "batch": 3})
RETRY_DELAYS_S = (10, 30, 120, 600)     # after the 1st, 2nd, 3rd, 4th failed try; the 5th failed try gives up


DELIVERY_RETRY_DELAY = timedelta(minutes=15)     # sending a result to the wrapper: the same wait every time ...
DELIVERY_TRIES = 10                              # ... and the 10th failed try gives up


def retry_after(failed_attempts: int) -> timedelta | None:
    """The S3 upload: how long to wait after ``failed_attempts`` failures in a row; ``None`` = give up and mark the
    work failed."""
    if failed_attempts < 1:
        raise ValueError("failed_attempts counts failures, so it starts at 1")
    if failed_attempts > len(RETRY_DELAYS_S):
        return None
    return timedelta(seconds=RETRY_DELAYS_S[failed_attempts - 1])


def delivery_retry_after(failed_attempts: int) -> timedelta | None:
    """Sending a result to the wrapper: wait 15 minutes after each failed try; ``None`` after the 10th = give up."""
    if failed_attempts < 1:
        raise ValueError("failed_attempts counts failures, so it starts at 1")
    return None if failed_attempts >= DELIVERY_TRIES else DELIVERY_RETRY_DELAY
