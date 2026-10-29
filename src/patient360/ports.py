"""The few things the core flow asks of the outside world. Real versions come later; tests use ``tests/fakes.py``."""
from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from patient360.domain.rules.packages import Package
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
class StoredSymptomCheck:
    visit_id: int
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

    def parameter_categories(self) -> Mapping[int, Collection[int]]:
        """parameter_id -> its list of category ids (``parameters.category_id``). Both maps come from
        ``build_catalogue_maps``, which keeps only what is ``active``."""

    def tests_by_category(self) -> Mapping[int, Sequence[CatalogueTest]]:
        """category_id -> the tests that list it in ``tests.category_id`` (a test can be in several categories)."""

    def test_names_ar(self) -> Mapping[str, str]:
        """English test name -> Arabic test name (``tests.test_name_ar``), for the Arabic html. All tests, active or
        not: a result saved when a test was active still needs its name."""

    def packages(self) -> Sequence[Package]:
        """The active packages, each with the ids of its active tests."""

    def test_ids_by_name(self) -> Mapping[str, int]:
        """``normalize_name`` of an English or Arabic test name -> test id, for the active tests."""

    def test_names(self) -> Mapping[int, tuple[str, str | None]]:
        """test id -> (English name, Arabic name), for every test: a package shows the names of its tests."""


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

    def visits_waiting_for_processing(self, received_before: datetime) -> list[int]:
        """Ids of visits still at stage ``received`` that arrived before ``received_before``, oldest first. These are
        visits whose processing was cut short (a restart), not the ones being processed right now."""

    def find_visit(self, idempotency_key: str) -> StoredVisit | None:
        """The visit that owns this key (AWS calls it ``recordId``), or ``None``."""

    def has_output(self, visit_id: int, analysis: Literal["visit", "symptom"] = "visit") -> bool:
        """Was the AWS output of this visit (or of its symptom check) already saved? A record can arrive twice."""

    def save_output(self, visit_id: int, internal_output: dict[str, Any], public_output: dict[str, Any],
                    analysis: Literal["visit", "symptom"] = "visit", internal_raw: bytes | None = None) -> None:
        """One transaction: both versions of the output (internal keeps model and usage), the rows of the answer
        (summary, recommendations, critical alerts, model call), stage ``post_processing`` (``_symptom``).
        ``internal_raw``: the record exactly as AWS sent it, kept as a text copy next to the structured one."""

    def mark_output_failed(self, visit_id: int, error_type: str,
                           analysis: Literal["visit", "symptom"] = "visit") -> None:
        """The AWS output could not be used: stage ``failed`` (``failed_symptom``). The file stays in Blob 2."""

    def save_failed_output(self, visit_id: int, public_output: dict[str, Any],
                           analysis: Literal["visit", "symptom"] = "visit") -> None:
        """One transaction, when the visit has no output yet (else nothing changes): the output row with status
        ``error``, the failed message as ``output_public``, a pending delivery, stage ``failed`` (``failed_symptom``).
        The normal sender then tells the Backend."""

    def stored_symptom_check(self, visit_id: int) -> StoredSymptomCheck:
        """The symptom check of a visit as received, with its raw bytes."""

    def visit_ai_answer(self, visit_id: int) -> dict[str, Any] | None:
        """The ``ai`` object of the visit's saved result (English and Arabic), or ``None`` while it has none."""

    def visit_model_input(self, visit_id: int) -> dict[str, Any]:
        """The Bedrock request the visit itself was processed into (its newest ``processed`` payload)."""

    def save_symptom_processed(self, visit_id: int, model_input: dict[str, Any],
                               read_from: Literal["blob", "postgresql"]) -> None:
        """One transaction: the symptom request ``model_input`` (analysis ``symptom``), stage ``processed_symptom``, and
        a pending work row."""

    def symptoms_waiting_for_processing(self, received_before: datetime) -> list[int]:
        """Visit ids whose symptom check is still at stage ``received_symptom`` and arrived before ``received_before``,
        oldest first: cut short by a restart, or waiting for the visit's own result."""

    def batch_fully_answered(self, batch_id: str) -> bool:
        """Does every visit of the batch have its output saved (a blocked one counts, a failed one does not)?"""

    def set_batch_status(
        self, batch_id: str, status: Literal["completed", "partially_completed", "failed"],
    ) -> None: ...

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

    def symptom_check_state(
        self, visit_id: int, patient_id: int,
    ) -> Literal["no_visit", "already_used", "closed", "ok"]:
        """``no_visit``: unknown visit or another patient; ``already_used``: its symptom check was received;
        ``closed``: the visit's saved result says ``symptom_flag`` false (a fully normal patient or a blocked result);
        ``ok`` otherwise, also while the visit has no result yet (the flag is not known: the check waits for it)."""

    def save_symptom_check(self, payload: dict[str, Any], raw: bytes, received_at: datetime,
                           read_from: Literal["blob", "postgresql"]) -> None: ...

    def save_rejected(self, source_id: int | None, raw: bytes, errors: list[dict[str, Any]],
                      blob_path: str | None) -> None: ...
