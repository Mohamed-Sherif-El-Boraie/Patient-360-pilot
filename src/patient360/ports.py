"""The few things the core flow asks of the outside world. Real versions come later; tests use ``tests/fakes.py``."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol


class BlobUnavailable(Exception):
    """Blob storage can not be reached right now (the flow falls back to PostgreSQL)."""


class BlobStore(Protocol):
    def put(self, path: str, data: bytes) -> bool:
        """Create the file. ``False`` if it already exists ("already stored"). Raises ``BlobUnavailable``."""


class VisitStore(Protocol):
    def visit_exists(self, visit_id: int) -> bool: ...

    def save_visit(self, payload: dict[str, Any], raw: bytes, idempotency_key: str, received_at: datetime,
                   read_from: Literal["blob", "postgresql"]) -> None:
        """Save the visit, its tests, results and the raw bytes in one transaction."""

    def symptom_check_state(self, visit_id: int, patient_id: int) -> Literal["no_visit", "already_used", "ok"]: ...

    def save_symptom_check(self, payload: dict[str, Any], raw: bytes, received_at: datetime,
                           read_from: Literal["blob", "postgresql"]) -> None: ...

    def save_rejected(self, source_id: int | None, raw: bytes, errors: list[dict[str, Any]],
                      blob_path: str | None) -> None: ...
