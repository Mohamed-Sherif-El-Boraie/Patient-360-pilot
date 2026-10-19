"""Opaque record id: the same patient, visit and source always give the same id (a duplicate)."""
from __future__ import annotations

import hashlib
import hmac


def make_record_id(key: bytes, *, patient_id: int, visit_id: int, source: str) -> str:
    """HMAC (not plain SHA-256: short numeric ids can be guessed). Also the Backend idempotency key.

    ``source`` is part of the id because one visit can have two records: its normal visit record and its
    symptom check.
    """
    message = f"{patient_id}|{visit_id}|{source}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:32]
