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
    "AZURE_STORAGE_ACCOUNT_URL", "BLOB_INPUT_CONTAINER", "BLOB_OUTPUT_CONTAINER", "IDEMPOTENCY_KEY_SECRET",
)
OPTIONAL = (
    "APP_ENV", "TELEMETRY_ENABLED", "LOG_LEVEL", "LOG_FORMAT", "PGPORT", "PGSSLMODE", "AWS_REGION", "AWS_ROLE_ARN",
    "BACKEND_RESULTS_URL", "BACKEND_CONNECT_TIMEOUT_S", "BACKEND_READ_TIMEOUT_S", "MAX_REQUEST_BYTES", "BATCH_SIZE",
)
APP_ENVS = ("development", "pilot", "production")   # pilot logs like production: INFO, json
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
LOG_FORMATS = ("console", "json")
MIN_SECRET_LENGTH = 16
DEFAULT_BATCH_SIZE = 100
MIN_BATCH_SIZE, MAX_BATCH_SIZE = 100, 50_000       # the limits of one Bedrock batch inference job in our project


class SettingsError(ValueError):
    """The settings are missing or wrong. The message holds names only."""


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
    azure_storage_account_url: str
    blob_input_container: str
    blob_output_container: str
    backend_connect_timeout_s: float
    backend_read_timeout_s: float
    pg_password: str = field(repr=False)
    idempotency_key_secret: str = field(repr=False)
    aws_region: str | None = None
    s3_buckets: tuple[tuple[str, str], ...] = ()   # (source, bucket name), only the buckets that are set
    aws_role_arn: str | None = None
    backend_results_url: str | None = None
    max_request_bytes: int | None = None            # None = no limit, until we know the real size of the Backend JSONs
    batch_size: int = DEFAULT_BATCH_SIZE            # visits per batch (per lane), 100 to 50000; change it in .env


S3_SETTINGS = {
    "on-demand": "S3_BUCKET_ON_DEMAND", "urgent": "S3_BUCKET_URGENT",
    "symptom-checker": "S3_BUCKET_SYMPTOM_CHECKER", "batch": "S3_BUCKET_BATCH",
}


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    values = {name: str(env.get(name, "")).strip() for name in (*REQUIRED, *OPTIONAL, *S3_SETTINGS.values())}
    problems = [f"{name} is missing" for name in REQUIRED if not values[name]]

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
    secret = values["IDEMPOTENCY_KEY_SECRET"]
    if secret and len(secret) < MIN_SECRET_LENGTH:
        problems.append(f"IDEMPOTENCY_KEY_SECRET is too short (at least {MIN_SECRET_LENGTH} characters)")
    if problems:
        raise SettingsError("; ".join(problems))
    return Settings(
        app_env=app_env, telemetry_enabled=telemetry_enabled, log_level=log_level, log_format=log_format,
        pg_host=values["PGHOST"], pg_port=pg_port, pg_database=values["PGDATABASE"], pg_user=values["PGUSER"],
        pg_password=values["PGPASSWORD"], pg_ssl_mode=values["PGSSLMODE"] or "verify-full",
        azure_storage_account_url=values["AZURE_STORAGE_ACCOUNT_URL"],
        blob_input_container=values["BLOB_INPUT_CONTAINER"], blob_output_container=values["BLOB_OUTPUT_CONTAINER"],
        idempotency_key_secret=secret,
        backend_connect_timeout_s=connect_timeout, backend_read_timeout_s=read_timeout,
        aws_region=values["AWS_REGION"] or None, aws_role_arn=values["AWS_ROLE_ARN"] or None,
        s3_buckets=tuple((source, values[name]) for source, name in S3_SETTINGS.items() if values[name]),
        backend_results_url=values["BACKEND_RESULTS_URL"] or None, max_request_bytes=max_request_bytes,
        batch_size=batch_size,
    )
