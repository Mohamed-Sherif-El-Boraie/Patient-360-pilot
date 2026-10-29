import json

import pytest

from patient360.config.settings import load_settings
from patient360.flow.intake import Accepted
from patient360.runtime import build_services
from tests.fakes import FakeBackend, FakeBlobs, FakeS3

ENV = {
    "PGHOST": "127.0.0.1", "PGDATABASE": "d", "PGUSER": "u", "PGPASSWORD": "p", "AZURE_STORAGE_CONNECTION_STRING": "c",
    "BLOB_INPUT_CONTAINER": "in", "IDEMPOTENCY_KEY_SECRET": "x" * 20,
}


def test_the_built_services_accept_a_visit_into_the_database(db, sample):
    services = build_services(load_settings(ENV), db=db, blob=FakeBlobs(), s3=FakeS3(), backend=FakeBackend())
    visit = sample("urgent_mild")
    outcome = services.intake.receive(json.dumps(visit).encode())
    assert isinstance(outcome, Accepted) and services.sender is not None
    with db.transaction() as conn:
        found = conn.execute("SELECT count(*) AS n FROM visits WHERE visit_id = %s", (visit["visit_id"],)).fetchone()
    assert found["n"] == 1


def test_without_a_backend_there_is_no_sender(db):
    services = build_services(load_settings(ENV), db=db, blob=FakeBlobs(), s3=FakeS3())
    assert services.sender is None


def test_a_missing_s3_bucket_stops_the_start_up_and_names_the_source(db):
    with pytest.raises(ValueError, match="batch"):
        build_services(load_settings(ENV | {"S3_BUCKET_URGENT": "u"}), db=db, blob=FakeBlobs())
