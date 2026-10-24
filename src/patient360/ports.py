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

    def get(self, path: str) -> bytes | None:
        """The file, or ``None`` if it is not there. Raises ``BlobUnavailable``."""


class S3Unavailable(Exception):
    """S3 can not be reached, or refused the file (the dispatcher retries later)."""


class S3Store(Protocol):
    def put(self, source: str, key: str, data: bytes) -> None:
        """Upload to the bucket of ``source``. Writing here starts the AWS automation. Raises ``S3Unavailable``."""


class BackendUnavailable(Exception):
    """A timeout, no connection, 429 or a 5xx from the Backend: the same call may work later."""


class BackendRejected(Exception):
    """The Backend answered 4xx (not 429): the same body would be refused again, so we do not retry."""


class BackendClient(Protocol):
    def post_result(self, body: dict[str, Any]) -> None:
        """One result, one call. Raises ``BackendUnavailable`` or ``BackendRejected``."""


@dataclass(frozen=True)
class DeliveryUnit:
    """One result ready to be sent to the Backend."""
    visit_id: int
    source: str                                   # urgent, on-demand, symptom-checker or batch
    attempts: int                                 # failed tries so far
    analysis: Literal["visit", "symptom"] = "visit"


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
    created_at: datetime          # the database time of the batch: its day names the folders


@dataclass(frozen=True)
class WorkUnit:
    """One thing to upload to S3: a visit's file, a symptom check's file, or a whole batch (its JSONL)."""
    source: str                                   # urgent, on-demand, symptom-checker or batch
    lane: Literal["normal", "critical"]
    created_at: datetime                          # when the visit was received, or the batch was made
    read_from: Literal["blob", "postgresql"]
    attempts: int                                 # failed tries so far
    visit_id: int | None = None                   # None for a batch
    idempotency_key: str | None = None            # None for a batch
    batch_id: str | None = None
    analysis: Literal["visit", "symptom"] = "visit"


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

    def claim_next_work(self, now: datetime) -> WorkUnit | None:
        """The next unit to upload, best source first (urgent, symptom-checker, on-demand, batch), oldest first; units
        waiting for a retry are skipped until their time. One transaction with ``FOR UPDATE SKIP LOCKED``: a unit is
        claimed by one worker only. Marks it ``queued`` and ``processing``."""

    def model_input_of(self, unit: WorkUnit) -> dict[str, Any]:
        """The saved Bedrock request of a visit or symptom check (the database copy)."""

    def batch_model_inputs(self, batch_id: str) -> list[BatchVisit]:
        """Every visit of a batch with its saved request, in the order of the batch: to rebuild a lost JSONL."""

    def mark_sent(self, unit: WorkUnit) -> None:
        """Stage ``sent_to_aws`` (and the batch ``submitted``), work row ``sent``, the attempt recorded."""

    def mark_failed_attempt(self, unit: WorkUnit, error_type: str, retry_at: datetime | None) -> None:
        """Count the failure and release the unit for ``retry_at``; ``None`` = give up: stage ``failed``."""

    def claim_next_delivery(self, now: datetime) -> DeliveryUnit | None:
        """The next result to send, best source first (same order as the dispatcher), oldest first; results waiting for
        a retry are skipped until their time. One worker per result (``FOR UPDATE SKIP LOCKED``)."""

    def public_output_of(self, unit: DeliveryUnit) -> dict[str, Any]:
        """The result exactly as the Backend gets it (the saved ``output_public``)."""

    def mark_delivered(self, unit: DeliveryUnit) -> None:
        """Stage ``delivered`` (``delivered_symptom``), work row ``delivered``, the attempt recorded."""

    def mark_delivery_failed_attempt(self, unit: DeliveryUnit, error_type: str, retry_at: datetime | None) -> None:
        """Count the failure and release the result for ``retry_at``; ``None`` = give up: stage ``failed``."""

    def set_batch_read_from(self, batch_id: str, read_from: Literal["blob", "postgresql"]) -> None:
        """Where the batch's visits are read from once the batch files were (or could not be) written."""

    def symptom_check_state(self, visit_id: int, patient_id: int) -> Literal["no_visit", "already_used", "ok"]: ...

    def save_symptom_check(self, payload: dict[str, Any], raw: bytes, received_at: datetime,
                           read_from: Literal["blob", "postgresql"]) -> None: ...

    def save_rejected(self, source_id: int | None, raw: bytes, errors: list[dict[str, Any]],
                      blob_path: str | None) -> None: ...
