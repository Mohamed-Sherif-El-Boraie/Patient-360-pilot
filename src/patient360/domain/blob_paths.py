"""Where each file goes in Azure Blob and S3 (the layout of tests/fixtures/flow/Buckets-Structure.md). Pure.

The folders are the source, the lane and the date (``yyyy/MM/dd``); the file is named by the full idempotency key.
Every name is built here, so nothing else concatenates paths.
Names never hold a patient number; only ``rejected_path`` holds a visit number, because a rejected payload may have
no key yet.

The date folder is the day the visit was RECEIVED (UTC), so all files of a visit share one date: ``blob_day()``.
"""
from __future__ import annotations

import re
from datetime import UTC, date, datetime

SOURCES = frozenset({"on-demand", "urgent", "batch", "symptom-checker"})
LANES = frozenset({"normal", "critical"})
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,100}$")   # no slash, no dot: a name can never climb out of its folder


def blob_day(moment: datetime) -> date:
    """The date folder for a moment (a timestamp with a time zone): its UTC date."""
    if moment.tzinfo is None:
        raise ValueError("the time needs a time zone")
    return moment.astimezone(UTC).date()


def _check(source: str, lane: str | None = None) -> None:
    if source not in SOURCES:
        raise ValueError(f"unknown source {source!r}")
    if lane is not None and lane not in LANES:
        raise ValueError(f"unknown lane {lane!r}")


def _name(value: str, what: str) -> str:
    if not isinstance(value, str) or not _SAFE_NAME.match(value):
        raise ValueError(f"{what} must be letters, digits, _ or -")
    return value


def _key(idempotency_key: str) -> str:
    return _name(idempotency_key, "idempotency_key")


def _day(day: date) -> str:
    return f"{day.year:04d}/{day.month:02d}/{day.day:02d}"


def _batch_rules(source: str, batch_id: str | None) -> None:
    if source == "batch" and batch_id is None:
        raise ValueError("a batch file needs its batch_id")
    if source != "batch" and batch_id is not None:
        raise ValueError("only a batch file has a batch_id")


def raw_path(source: str, day: date, idempotency_key: str) -> str:
    """Azure Blob 1: the JSON exactly as the Backend sent it."""
    _check(source)
    return f"raw/{source}/{_day(day)}/{_key(idempotency_key)}.json"


def rejected_path(source: str, day: date, visit_id: int) -> str:
    """Azure Blob 1: a payload that failed validation. It may have no key yet, so the visit id names it."""
    _check(source)
    if not isinstance(visit_id, int) or isinstance(visit_id, bool) or visit_id < 1:
        raise ValueError("visit_id must be a positive whole number")
    return f"raw/_rejected/{source}/{_day(day)}/{visit_id}.json"


def rejected_unknown_path(day: date, raw_sha256_hex: str) -> str:
    """Azure Blob 1: a rejected payload with no usable source or visit id; named by a hash of its bytes."""
    return f"raw/_rejected/unknown/{_day(day)}/unknown_{_name(raw_sha256_hex[:16], 'hash')}.json"


def processed_path(source: str, lane: str, day: date, idempotency_key: str, batch_id: str | None = None) -> str:
    """Azure Blob 1: the JSON prepared for the AI model. A batch visit sits inside its batch folder."""
    _check(source, lane)
    _batch_rules(source, batch_id)
    key = _key(idempotency_key)
    if batch_id is not None:
        return f"processed/batch/{lane}/{_day(day)}/{_name(batch_id, 'batch_id')}/{key}.json"
    return f"processed/{source}/{lane}/{_day(day)}/{key}.json"


def batch_jsonl_path(lane: str, day: date, batch_id: str) -> str:
    """Azure Blob 1: the 100-visit file for AWS."""
    _check("batch", lane)
    return f"processed/batch/{lane}-jsonls/{_day(day)}/{_name(batch_id, 'batch_id')}.jsonl"


def merged_output_path(source: str, lane: str, day: date, idempotency_key: str, batch_id: str | None = None) -> str:
    """Azure Blob 2: the final merged result of one visit (English and Arabic)."""
    _check(source, lane)
    _batch_rules(source, batch_id)
    key = _key(idempotency_key)
    if batch_id is not None:
        return f"aws-output-merged/batch/{lane}/{_day(day)}/{_name(batch_id, 'batch_id')}/{key}.json"
    return f"aws-output-merged/{source}/{lane}/{_day(day)}/{key}.json"


def merged_output_jsonl_path(lane: str, day: date, batch_id: str) -> str:
    """Azure Blob 2: the merged results of a whole batch."""
    _check("batch", lane)
    return f"aws-output-merged/batch/{lane}-jsonls/{_day(day)}/{_name(batch_id, 'batch_id')}.jsonl"


def s3_processed_key(source: str, lane: str, day: date, idempotency_key: str, batch_id: str | None = None) -> str:
    """The key inside the S3 bucket of this source (one bucket per source). A batch uploads its JSONL."""
    _check(source, lane)
    _batch_rules(source, batch_id)
    key = _key(idempotency_key)
    if batch_id is not None:
        return f"processed/{lane}-jsonls/{_day(day)}/{_name(batch_id, 'batch_id')}.jsonl"
    return f"processed/{lane}/{_day(day)}/{key}.json"
