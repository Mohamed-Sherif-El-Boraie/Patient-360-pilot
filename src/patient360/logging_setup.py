"""Logging with structlog. Every log line passes an allow-list: only the fields below can ever be written, at any level.

Patient data (patient or visit numbers, names, symptoms, values, exception text) is not on the list, so it is dropped
even if a developer passes it by mistake. To trace a record, bind its opaque key once per record with
``structlog.contextvars.bind_contextvars(idempotency_key=...)`` and call ``clear_contextvars()`` when it is done.
"""
from __future__ import annotations

import logging
from typing import TextIO

import structlog

ALLOWED_FIELDS = frozenset({
    "event", "level", "timestamp", "idempotency_key", "source", "lane", "tier", "stage", "phase", "attempt",
    "duration_ms", "status", "error_type",
})


def _only_allowed(logger, method_name, event_dict):
    return {key: value for key, value in event_dict.items() if key in ALLOWED_FIELDS}


def configure_logging(level: str, log_format: str, stream: TextIO | None = None) -> None:
    """``level``: DEBUG, INFO, WARNING or ERROR. ``log_format``: ``json`` or ``console``. ``stream``: for tests."""
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ValueError(f"unknown log level {level!r}")
    if log_format not in ("json", "console"):
        raise ValueError(f"unknown log format {log_format!r}")
    renderer = structlog.processors.JSONRenderer() if log_format == "json" else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _only_allowed,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        logger_factory=structlog.PrintLoggerFactory(file=stream),
        cache_logger_on_first_use=False,
    )
