"""The few things the core flow asks of the outside world. Real versions come later; tests use ``tests/fakes.py``."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from patient360.domain.rules.retrieval import CatalogueTest


class BlobUnavailable(Exception):
    """Blob storage can not be reached right now (the flow falls back to PostgreSQL)."""


class BlobStore(Protocol):
    def put(self, path: str, data: bytes) -> bool:
        """Create the file. ``False`` if it already exists ("already stored"). Raises ``BlobUnavailable``."""


@dataclass(frozen=True)
class StoredVisit:
    visit_id: int
    idempotency_key: str
    source_id: int
    received_at: datetime
    raw: bytes


@dataclass(frozen=True)
class BatchVisit:
    idempotency_key: str
    model_input: dict[str, Any]


@dataclass(frozen=True)
class ClaimedBatch:
    batch_id: str
    visits: list[BatchVisit]


class Catalogue(Protocol):
    """The reference data behind the retrieval rule."""

    def parameter_categories(self) -> Mapping[int, str | None]: ...

    def tests_by_category(self) -> Mapping[str, Sequence[CatalogueTest]]: ...


class VisitStore(Protocol):
    def visit_exists(self, visit_id: int) -> bool: ...

    def stored_visit(self, visit_id: int) -> StoredVisit:
        """The visit as received, with its raw bytes."""

    def save_processed(self, visit_id: int, tier: str, model_input: dict[str, Any],
                       read_from: Literal["blob", "postgresql"]) -> None:
        """One transaction: the tier, the Bedrock request ``model_input`` (new version), stage ``processed``, and a
        pending work row."""

    def save_visit(self, payload: dict[str, Any], raw: bytes, idempotency_key: str, received_at: datetime,
                   read_from: Literal["blob", "postgresql"]) -> None:
        """Save the visit, its tests, results and the raw bytes in one transaction."""

    def claim_batch(self, lane: Literal["normal", "critical"], size: int) -> ClaimedBatch | None:
        """One transaction: if ``size`` processed batch visits of the lane wait, take the oldest ``size`` (rows locked,
        so two workers never take the same visit), create the ``batches`` row (next id of the lane's sequence), mark
        the visits batched and ``queued``. ``None`` when fewer wait."""

    def set_batch_read_from(self, batch_id: str, read_from: Literal["blob", "postgresql"]) -> None:
        """Where the batch's visits are read from once the batch files were (or could not be) written."""

    def symptom_check_state(self, visit_id: int, patient_id: int) -> Literal["no_visit", "already_used", "ok"]: ...

    def save_symptom_check(self, payload: dict[str, Any], raw: bytes, received_at: datetime,
                           read_from: Literal["blob", "postgresql"]) -> None: ...

    def save_rejected(self, source_id: int | None, raw: bytes, errors: list[dict[str, Any]],
                      blob_path: str | None) -> None: ...
