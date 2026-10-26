"""All configuration comes from environment variables (a local ``.env`` file or the App Service settings).

``load_settings`` reads them once and checks them all; a mistake lists every problem by NAME, never by value, so a
secret can not end up in an error message or a log.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

REQUIRED = (
    "PGHOST", "PGDATABASE", "PGUSER", "PGPASSWORD",
    "BLOB_INPUT_CONTAINER", "BLOB_OUTPUT_CONTAINER", "IDEMPOTENCY_KEY_SECRET",
)
BLOB_ACCESS = ("AZURE_STORAGE_ACCOUNT_URL", "AZURE_STORAGE_CONNECTION_STRING")   # one of the two is required
OPTIONAL = (
    "APP_ENV", "TELEMETRY_ENABLED", "LOG_LEVEL", "LOG_FORMAT", "PGPORT", "PGSSLMODE", "AWS_REGION", "AWS_ROLE_ARN",
    "AWS_ENDPOINT_URL_S3", "BACKEND_CONNECT_TIMEOUT_S", "BACKEND_READ_TIMEOUT_S", "MAX_REQUEST_BYTES", "BATCH_SIZE",
    "INBOUND_API_KEY", "JOB_INTERVAL_S", "WORK_INTERVAL_S", "EVENT_QUEUE_NAME",
)
BACKEND_SETTINGS = (          # the wrapper: all six or none
    "BACKEND_BASE_URL", "BACKEND_CALLER_SOURCE", "BACKEND_CALLER_SOURCE_KEY", "BACKEND_CALLER_USER_NAME",
    "BACKEND_USERNAME", "BACKEND_PASSWORD",
)
APP_ENVS = ("development", "pilot", "production")   # pilot logs like production: INFO, json
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
LOG_FORMATS = ("console", "json")
MIN_SECRET_LENGTH = 16
DEFAULT_BATCH_SIZE = 100
MIN_BATCH_SIZE, MAX_BATCH_SIZE = 100, 50_000       # the limits of one Bedrock batch inference job in our project
DEFAULT_JOB_INTERVAL_S = 300                       # the batch trigger and the sweeps run every 5 minutes
DEFAULT_WORK_INTERVAL_S = 5                        # uploading to S3 and delivering to the Backend run every 5 seconds


class SettingsError(ValueError):
    """The settings are missing or wrong. The message holds names only."""


@dataclass(frozen=True)
class BackendSettings:
    """How to reach the Backend wrapper: where, who we are (caller headers) and the login."""
    base_url: str
    caller_source: str
    caller_user_name: str
    username: str
    caller_source_key: str = field(repr=False)
    password: str = field(repr=False)


@dataclass(frozen=True)
class Settings:
    app_env: str
    telemetry_enabled: bool     # OpenTelemetry on or off (off until we switch it on)
    log_level: str
    log_format: str
    pg_host: str
    pg_port: int
    pg_database: str
    pg_user: str
    pg_ssl_mode: str
    blob_input_container: str
    blob_output_container: str
    backend_connect_timeout_s: float
    backend_read_timeout_s: float
    pg_password: str = field(repr=False)
    idempotency_key_secret: str = field(repr=False)
    azure_storage_account_url: str | None = None            # production: sign in with the managed identity
    azure_storage_connection_string: str | None = field(default=None, repr=False)   # local: the Azurite emulator
    aws_region: str | None = None
    aws_endpoint_url: str | None = None                      # local: a MinIO server instead of AWS S3
    s3_buckets: tuple[tuple[str, str], ...] = ()   # (source, bucket name), only the buckets that are set
    aws_role_arn: str | None = None
    backend: BackendSettings | None = None
    event_queue_name: str | None = None             # the storage queue Event Grid writes to; None = no queue is read
    max_request_bytes: int | None = None            # None = no limit, until we know the real size of the Backend JSONs
    batch_size: int = DEFAULT_BATCH_SIZE            # visits per batch (per lane), 100 to 50000; change it in .env
    inbound_api_key: str | None = field(default=None, repr=False)   # callers must send it; None = not checked
    job_interval_s: float = DEFAULT_JOB_INTERVAL_S
    work_interval_s: float = DEFAULT_WORK_INTERVAL_S


S3_SETTINGS = {
    "on-demand": "S3_BUCKET_ON_DEMAND", "urgent": "S3_BUCKET_URGENT",
    "symptom-checker": "S3_BUCKET_SYMPTOM_CHECKER", "batch": "S3_BUCKET_BATCH",
}


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    names = (*REQUIRED, *BLOB_ACCESS, *OPTIONAL, *BACKEND_SETTINGS, *S3_SETTINGS.values())
    values = {name: str(env.get(name, "")).strip() for name in names}
    problems = [f"{name} is missing" for name in REQUIRED if not values[name]]
    if not any(values[name] for name in BLOB_ACCESS):
        problems.append(f"{' or '.join(BLOB_ACCESS)} is missing")

    def choice(name: str, allowed: tuple[str, ...], default: str, upper: bool = False) -> str:
        value = values[name].upper() if upper else values[name].lower()
        if not value:
            return default
        if value not in allowed:
            problems.append(f"{name} must be one of {', '.join(allowed)}")
            return default
        return value

    def number(name: str, default: float, kind=float, low: float = 0, high: float | None = None):
        if not values[name]:
            return default
        try:
            result = kind(values[name])
        except ValueError:
            problems.append(f"{name} must be a number")
            return default
        if result <= low or (high is not None and result > high):
            problems.append(f"{name} is out of range")
            return default
        return result

    app_env = choice("APP_ENV", APP_ENVS, "production")
    telemetry_enabled = choice("TELEMETRY_ENABLED", ("true", "false"), "false") == "true"
    development = app_env == "development"
    log_level = choice("LOG_LEVEL", LOG_LEVELS, "DEBUG" if development else "INFO", upper=True)
    log_format = choice("LOG_FORMAT", LOG_FORMATS, "console" if development else "json")
    pg_port = number("PGPORT", 5432, int, high=65535)
    connect_timeout = number("BACKEND_CONNECT_TIMEOUT_S", 5.0)
    read_timeout = number("BACKEND_READ_TIMEOUT_S", 30.0)
    max_request_bytes = number("MAX_REQUEST_BYTES", None, int)
    batch_size = number("BATCH_SIZE", DEFAULT_BATCH_SIZE, int, low=MIN_BATCH_SIZE - 1, high=MAX_BATCH_SIZE)
    job_interval = number("JOB_INTERVAL_S", DEFAULT_JOB_INTERVAL_S)
    work_interval = number("WORK_INTERVAL_S", DEFAULT_WORK_INTERVAL_S)
    secret = values["IDEMPOTENCY_KEY_SECRET"]
    if secret and len(secret) < MIN_SECRET_LENGTH:
        problems.append(f"IDEMPOTENCY_KEY_SECRET is too short (at least {MIN_SECRET_LENGTH} characters)")
    backend_given = [name for name in BACKEND_SETTINGS if values[name]]
    backend = None
    if backend_given and len(backend_given) < len(BACKEND_SETTINGS):
        problems.extend(f"{name} is missing (the Backend settings go together)"
                        for name in BACKEND_SETTINGS if not values[name])
    elif backend_given:
        backend = BackendSettings(
            base_url=values["BACKEND_BASE_URL"].rstrip("/"), caller_source=values["BACKEND_CALLER_SOURCE"],
            caller_user_name=values["BACKEND_CALLER_USER_NAME"], username=values["BACKEND_USERNAME"],
            caller_source_key=values["BACKEND_CALLER_SOURCE_KEY"], password=values["BACKEND_PASSWORD"])
    if problems:
        raise SettingsError("; ".join(problems))
    return Settings(
        app_env=app_env, telemetry_enabled=telemetry_enabled, log_level=log_level, log_format=log_format,
        pg_host=values["PGHOST"], pg_port=pg_port, pg_database=values["PGDATABASE"], pg_user=values["PGUSER"],
        pg_password=values["PGPASSWORD"], pg_ssl_mode=values["PGSSLMODE"] or "verify-full",
        azure_storage_account_url=values["AZURE_STORAGE_ACCOUNT_URL"] or None,
        azure_storage_connection_string=values["AZURE_STORAGE_CONNECTION_STRING"] or None,
        blob_input_container=values["BLOB_INPUT_CONTAINER"], blob_output_container=values["BLOB_OUTPUT_CONTAINER"],
        idempotency_key_secret=secret,
        backend_connect_timeout_s=connect_timeout, backend_read_timeout_s=read_timeout,
        aws_region=values["AWS_REGION"] or None, aws_role_arn=values["AWS_ROLE_ARN"] or None,
        aws_endpoint_url=values["AWS_ENDPOINT_URL_S3"] or None,
        s3_buckets=tuple((source, values[name]) for source, name in S3_SETTINGS.items() if values[name]),
        backend=backend, max_request_bytes=max_request_bytes, batch_size=batch_size,
        event_queue_name=values["EVENT_QUEUE_NAME"] or None,
        inbound_api_key=values["INBOUND_API_KEY"] or None,
        job_interval_s=job_interval, work_interval_s=work_interval,
    )
