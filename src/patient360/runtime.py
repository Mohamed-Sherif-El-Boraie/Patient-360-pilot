"""Builds the running service: connects the flow classes to PostgreSQL, Blob, S3 and the Backend.

Every connection can be passed in, so tests and the local simulation replace the real ones.
"""
from __future__ import annotations

from datetime import UTC, datetime

import structlog
from psycopg.conninfo import make_conninfo

from patient360.adapters.blob import (
    build_blob_stores,
    build_blob_stores_from_connection_string,
    build_output_store,
)
from patient360.adapters.postgres.catalogue import PostgresCatalogue
from patient360.adapters.postgres.db import Database
from patient360.adapters.postgres.store import PostgresStore
from patient360.adapters.s3 import S3Uploader, build_s3_client
from patient360.config.settings import Settings
from patient360.flow.batching import BatchTrigger
from patient360.flow.dispatch import Dispatcher
from patient360.flow.intake import Intake
from patient360.flow.parked import ParkedRecovery
from patient360.flow.process import Processor
from patient360.flow.results import OutputReceiver
from patient360.flow.send_result import ResultSender
from patient360.flow.symptom import SymptomProcessor
from patient360.flow.unanswered import UnansweredSweep
from patient360.ports import BackendClient, BlobStore, S3Store
from patient360.services import Services

log = structlog.get_logger()


def _now() -> datetime:
    return datetime.now(UTC)


def raw_store(settings: Settings) -> BlobStore:
    """Blob 1 (``p360-raw``), the way the settings say. Also used by ``tools/trace_visit.py`` to list ``_pending``."""
    if settings.blob_raw_url:
        return build_output_store(settings.blob_raw_url)
    if settings.azure_storage_connection_string:
        return build_blob_stores_from_connection_string(
            settings.azure_storage_connection_string, settings.blob_input_container)
    return build_blob_stores(settings.azure_storage_account_url, settings.blob_input_container)


def build_services(
    settings: Settings, *, db: Database | None = None,
    blob: BlobStore | None = None, s3: S3Store | None = None, backend: BackendClient | None = None,
) -> Services:
    if db is None:
        db = Database.connect(make_conninfo(
            host=settings.pg_host, port=settings.pg_port, dbname=settings.pg_database, user=settings.pg_user,
            password=settings.pg_password, sslmode=settings.pg_ssl_mode,
            options=f"-c search_path={settings.pg_schema}" if settings.pg_schema else None),
            max_size=settings.db_pool_max_size)
    if blob is None:
        blob = raw_store(settings)
    if s3 is None:
        s3 = S3Uploader(build_s3_client(settings.aws_region, settings.aws_endpoint_url), dict(settings.s3_buckets))
    if backend is None and settings.backend is not None:
        from patient360.adapters.backend import WrapperBackend  # local: only needed when a Backend is configured
        backend = WrapperBackend(settings.backend, settings.backend_connect_timeout_s, settings.backend_read_timeout_s)
    if backend is None:
        log.warning("backend_not_configured")
    store, catalogue = PostgresStore(db), PostgresCatalogue(db)
    processor = Processor(store, catalogue, blob)
    intake = Intake(store, blob, settings.idempotency_key_secret.encode(), _now, settings.max_request_bytes,
                    prepare=processor.prepare)
    receiver = OutputReceiver(
        store, catalogue, {source: build_output_store(url) for source, url in settings.output_container_urls})
    return Services(
        settings=settings, db=db, store=store,
        intake=intake, parked=ParkedRecovery(intake, blob),
        processor=processor,
        symptom_processor=SymptomProcessor(store, blob),
        batch_trigger=BatchTrigger(store, blob, settings.batch_size),
        dispatcher=Dispatcher(store, blob, s3, _now),
        receiver=receiver,
        sender=ResultSender(store, backend, _now) if backend is not None else None,
        unanswered=UnansweredSweep(store, receiver, settings.aws_answer_timeout_s, settings.aws_check_interval_s),
    )
