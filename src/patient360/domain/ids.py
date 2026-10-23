"""The idempotency key: one per visit. The same patient and visit always give the same key (a duplicate)."""
from __future__ import annotations

import hashlib
import hmac


def make_idempotency_key(key: bytes, *, patient_id: int, visit_id: int) -> str:
    """HMAC (not plain SHA-256: short numeric ids can be guessed). Sent to the Backend with the visit's results."""
    message = f"{patient_id}|{visit_id}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:32]
