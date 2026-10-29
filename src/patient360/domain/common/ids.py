"""The idempotency key: one per visit. The same patient, visit and company always give the same key (a duplicate)."""
from __future__ import annotations

import hashlib
import hmac


def make_idempotency_key(key: bytes, *, patient_id: int, visit_id: int, company_id: int) -> str:
    """HMAC of patient|visit|company (not plain SHA-256: short numeric ids can be guessed). Sent to the Backend
    with the visit's results."""
    message = f"{patient_id}|{visit_id}|{company_id}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:32]


def format_batch_id(lane: str, number: int) -> str:
    """``normal_000001``: the lane and the next number of that lane's sequence (matches the ``batches`` CHECK)."""
    if lane not in ("normal", "critical") or number < 1:
        raise ValueError("a batch id needs a lane and a positive number")
    return f"{lane}_{number:06d}"
