"""AWS S3 behind the ``S3Store`` port: one bucket per source. Writing a file here starts the AWS automation."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import boto3
import structlog
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from patient360.domain.common.blob_paths import SOURCES
from patient360.ports import S3Unavailable

log = structlog.get_logger()


class S3Uploader:
    def __init__(self, client: Any, buckets: Mapping[str, str]) -> None:
        """``buckets``: source name -> bucket name. All four sources need one: a missing one stops the app at start-up,
        not later in the middle of a patient's result."""
        missing = sorted(SOURCES - buckets.keys())
        if missing:
            raise ValueError(f"no S3 bucket is set for: {', '.join(missing)}")
        self._client, self._buckets = client, dict(buckets)

    def put(self, source: str, key: str, data: bytes) -> None:
        content_type = "application/x-ndjson" if key.endswith(".jsonl") else "application/json"
        try:
            self._client.put_object(Bucket=self._buckets[source], Key=key, Body=data, ContentType=content_type)
        except (BotoCoreError, ClientError) as error:
            log.warning("s3_unavailable", source=source, error_type=type(error).__name__)
            raise S3Unavailable from error


def build_s3_client(region: str | None = None) -> Any:
    """A boto3 S3 client with the default credential chain (environment, profile or role). Short timeouts and one
    retry: the dispatcher has its own retry schedule."""
    config = Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 2, "mode": "standard"})
    return boto3.client("s3", region_name=region, config=config)
