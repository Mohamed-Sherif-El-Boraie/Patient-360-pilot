"""Dispatch rules: which source goes to AWS first, and how long to wait before the next try. Pure."""
from __future__ import annotations

from datetime import timedelta

from patient360.domain.common.compat import frozendict

# Smaller number = sent first.
SOURCE_PRIORITY = frozendict({"urgent": 0, "symptom-checker": 1, "on-demand": 2, "batch": 3})
QUICK_RETRIES_S = (10, 30, 120, 600)          # after the 1st, 2nd, 3rd and 4th failed try
REPEAT_EVERY = timedelta(minutes=15)          # then the same wait again and again
UPLOAD_REPEATS = 10                           # the S3 upload repeats it 10 times, then gives up


def retry_after(failed_attempts: int) -> timedelta | None:
    """The S3 upload: 10 s, 30 s, 2 min, 10 min, then 10 tries 15 minutes apart. ``None`` after the 15th failed try
    (about 2 hours 43 minutes): give up and mark the work failed."""
    if failed_attempts < 1:
        raise ValueError("failed_attempts counts failures, so it starts at 1")
    if failed_attempts <= len(QUICK_RETRIES_S):
        return timedelta(seconds=QUICK_RETRIES_S[failed_attempts - 1])
    if failed_attempts <= len(QUICK_RETRIES_S) + UPLOAD_REPEATS:
        return REPEAT_EVERY
    return None


def delivery_retry_after(failed_attempts: int) -> timedelta:
    """Sending a result to the wrapper: 10 s, 30 s, 2 min, 10 min, then every 15 minutes with no limit, until the
    wrapper accepts it. (A result the wrapper refuses for good is not retried at all: see ``BackendRejected``.)"""
    if failed_attempts < 1:
        raise ValueError("failed_attempts counts failures, so it starts at 1")
    if failed_attempts <= len(QUICK_RETRIES_S):
        return timedelta(seconds=QUICK_RETRIES_S[failed_attempts - 1])
    return REPEAT_EVERY
