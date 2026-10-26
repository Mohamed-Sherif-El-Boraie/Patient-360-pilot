"""Builds the running service: connects the flow classes to PostgreSQL, Blob, S3 and the Backend.

Every connection can be passed in, so tests and the local simulation replace the real ones.
"""
from __future__ import annotations

from datetime import UTC, datetime

import structlog
from psycopg.conninfo import make_conninfo

from patient360.adapters.blob import build_blob_stores, build_blob_stores_from_connection_string
from patient360.adapters.postgres.catalogue import PostgresCatalogue
from patient360.adapters.postgres.db import Database
from patient360.adapters.postgres.store import PostgresStore
from patient360.adapters.s3 import S3Uploader, build_s3_client
from patient360.config.settings import Settings
from patient360.flow.batching import BatchTrigger
from patient360.flow.blob_events import BlobEventConsumer
from patient360.flow.dispatch import Dispatcher
from patient360.flow.intake import Intake
from patient360.flow.process import Processor
from patient360.flow.results import OutputReceiver
from patient360.flow.send_result import ResultSender
from patient360.flow.symptom import SymptomProcessor
from patient360.ports import BackendClient, BlobStore, EventQueue, S3Store
from patient360.services import Services

log = structlog.get_logger()


def _now() -> datetime:
    return datetime.now(UTC)


def build_services(
    settings: Settings, *, db: Database | None = None, blob_stores: tuple[BlobStore, BlobStore] | None = None,
    s3: S3Store | None = None, backend: BackendClient | None = None, event_queue: EventQueue | None = None,
) -> Services:
    if db is None:
        db = Database.connect(make_conninfo(
            host=settings.pg_host, port=settings.pg_port, dbname=settings.pg_database, user=settings.pg_user,
            password=settings.pg_password, sslmode=settings.pg_ssl_mode))
    if blob_stores is None:
        if settings.azure_storage_connection_string:
            blob_stores = build_blob_stores_from_connection_string(
                settings.azure_storage_connection_string, settings.blob_input_container,
                settings.blob_output_container)
        else:
            blob_stores = build_blob_stores(settings.azure_storage_account_url, settings.blob_input_container,
                                            settings.blob_output_container)
    if s3 is None:
        s3 = S3Uploader(build_s3_client(settings.aws_region, settings.aws_endpoint_url), dict(settings.s3_buckets))
    if backend is None and settings.backend is not None:
        from patient360.adapters.backend import WrapperBackend  # local: only needed when a Backend is configured
        backend = WrapperBackend(settings.backend, settings.backend_connect_timeout_s, settings.backend_read_timeout_s)
    if backend is None:
        log.warning("backend_not_configured")
    if event_queue is None and settings.event_queue_name:
        from patient360.adapters.storage_queue import build_event_queue  # local: only needed when a queue is configured
        event_queue = build_event_queue(
            settings.event_queue_name, connection_string=settings.azure_storage_connection_string,
            account_url=settings.azure_storage_account_url)
    blob_1, blob_2 = blob_stores
    store, catalogue = PostgresStore(db), PostgresCatalogue(db)
    receiver = OutputReceiver(store, blob_2, catalogue)
    return Services(
        settings=settings, db=db, store=store,
        intake=Intake(store, blob_1, settings.idempotency_key_secret.encode(), _now, settings.max_request_bytes),
        processor=Processor(store, catalogue, blob_1),
        symptom_processor=SymptomProcessor(store, catalogue, blob_1),
        batch_trigger=BatchTrigger(store, blob_1, settings.batch_size),
        dispatcher=Dispatcher(store, blob_1, s3, _now),
        receiver=receiver,
        sender=ResultSender(store, backend, _now) if backend is not None else None,
        event_consumer=BlobEventConsumer(event_queue, receiver) if event_queue is not None else None,
    )
