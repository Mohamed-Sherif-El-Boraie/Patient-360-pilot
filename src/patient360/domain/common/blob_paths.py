"""Where each file goes in Azure Blob and S3 (the layout of tests/fixtures/flow/Buckets-Structure.md). Pure.

The folders are the source, the lane and the date (``yyyy/MM/dd``); the file is named by the full idempotency key.
Every name is built here, so nothing else concatenates paths.
Names never hold a patient number; only ``rejected_path`` holds a visit number, because a rejected payload may have
no key yet.

The date folder is the day the visit was RECEIVED (UTC), so all files of a visit share one date: ``blob_day()``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal

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


# p360-raw has three main folders: raw-jsons (a folder per source inside), _rejected and processed.
RAW_FOLDER = "raw-jsons"


def raw_path(source: str, day: date, idempotency_key: str) -> str:
    """Azure Blob 1: the JSON exactly as the Backend sent it, in the folder of its source."""
    _check(source)
    return f"{RAW_FOLDER}/{source}/{_day(day)}/{_key(idempotency_key)}.json"


def rejected_path(source: str, day: date, visit_id: int) -> str:
    """Azure Blob 1: a payload that failed validation. It may have no key yet, so the visit id names it."""
    _check(source)
    if not isinstance(visit_id, int) or isinstance(visit_id, bool) or visit_id < 1:
        raise ValueError("visit_id must be a positive whole number")
    return f"_rejected/{source}/{_day(day)}/{visit_id}.json"


def rejected_unknown_path(day: date, raw_sha256_hex: str) -> str:
    """Azure Blob 1: a rejected payload with no usable source or visit id; named by a hash of its bytes."""
    return f"_rejected/unknown/{_day(day)}/unknown_{_name(raw_sha256_hex[:16], 'hash')}.json"


PENDING_PREFIX = "_pending/"


def pending_path(day: date, visit_id: int, raw_sha256_hex: str) -> str:
    """Azure Blob 1: a valid payload that could not be saved in the database. It waits here until the recovery saves it.
    The same bytes sent again make the same name, so a resend never makes a second file."""
    if not isinstance(visit_id, int) or isinstance(visit_id, bool) or visit_id < 1:
        raise ValueError("visit_id must be a positive whole number")
    return f"{PENDING_PREFIX}{_day(day)}/{visit_id}-{_name(raw_sha256_hex[:16], 'hash')}.json"


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


OUTPUT_FILE = "05-output-merged"      # the phase file AWS writes last: the English and Arabic answer together
FAILED_FILE = "06-failed"             # AWS writes this instead when it gave up on the visit (any step)


def merged_output_path(lane: str, day: date, idempotency_key: str) -> str:
    """The answer of one visit, inside the output container of its source."""
    _check("batch", lane)
    return f"{lane}/{_day(day)}/{_key(idempotency_key)}/{OUTPUT_FILE}.json"


def merged_output_jsonl_path(lane: str, day: date, batch_id: str) -> str:
    """The answers of a whole batch (one record per line), inside the batch output container."""
    _check("batch", lane)
    return f"{lane}-jsonls/{_day(day)}/{_name(batch_id, 'batch_id')}/{OUTPUT_FILE}.jsonl"


def backend_output_path(lane: str, day: date, idempotency_key: str, batch_id: str | None = None) -> str:
    """The body we send to the Backend, in the output container of its source. A batch visit sits inside its batch
    folder (AWS writes its own per-visit file outside it, which we do not control)."""
    _check("batch", lane)
    folder = f"{lane}/{_day(day)}"
    if batch_id is not None:
        folder += f"/{_name(batch_id, 'batch_id')}"
    return f"{folder}/{_key(idempotency_key)}/06-backend-output.json"


@dataclass(frozen=True)
class MergedOutput:
    """An answer file in an output container, read back from its path."""
    kind: Literal["visit", "batch_jsonl", "batch_visit", "failed", "batch_failed"]
    source: str
    lane: str
    day: date
    key: str | None = None            # the idempotency key, for a visit file
    batch_id: str | None = None


_NAME = r"([A-Za-z0-9_-]{1,100})"
_DATE = r"(\d{4})/(\d{2})/(\d{2})"
_VISIT_FILE = re.compile(rf"^(normal|critical)/{_DATE}/{_NAME}/{OUTPUT_FILE}\.json$")
_BATCH_JSONL = re.compile(rf"^(normal|critical)-jsonls/{_DATE}/{_NAME}/{OUTPUT_FILE}\.jsonl$")
_FAILED_FILE = re.compile(rf"^(normal|critical)/{_DATE}/{_NAME}/{FAILED_FILE}\.json$")
_BATCH_FAILED = re.compile(rf"^(normal|critical)-jsonls/{_DATE}/{_NAME}/{FAILED_FILE}\.jsonl$")   # one line per visit


def parse_output_path(source: str, path: str) -> MergedOutput | None:
    """What a path inside the output container of ``source`` is, or ``None`` for any other file (the other phase
    files, logs: not ours to handle). In the batch container a visit file is a copy of a line of the JSONL
    (``batch_visit``): the caller ignores it."""
    if source not in SOURCES:
        return None
    try:
        if m := _FAILED_FILE.match(path):       # a visit's own file in every source, a batch visit included
            lane, *day, key = m.groups()
            return MergedOutput("failed", source, lane, date(*map(int, day)), key=key)
        if m := _VISIT_FILE.match(path):
            lane, *day, key = m.groups()
            return MergedOutput("batch_visit" if source == "batch" else "visit", source, lane,
                                date(*map(int, day)), key=key)
        if source == "batch" and (m := _BATCH_JSONL.match(path)):
            lane, *day, batch_id = m.groups()
            return MergedOutput("batch_jsonl", source, lane, date(*map(int, day)), batch_id=batch_id)
        if source == "batch" and (m := _BATCH_FAILED.match(path)):     # the key of a line is not in the path
            lane, *day, batch_id = m.groups()
            return MergedOutput("batch_failed", source, lane, date(*map(int, day)), batch_id=batch_id)
    except ValueError:      # a date that does not exist, such as 2026/13/45
        return None
    return None


def s3_processed_key(
    source: str, lane: str, day: date, idempotency_key: str | None, batch_id: str | None = None,
) -> str:
    """The key inside the S3 bucket of this source (one bucket per source), the layout of Buckets-Structure.md:
    ``<lane>/<yyyy>/<MM>/<dd>/<idempotency_key>/01-input.json`` for a visit, and
    ``<lane>/<yyyy>/<MM>/<dd>/<batch_id>/01-input.jsonl`` for a batch. The only file we ever upload is ``01-input``
    (the IAM policy allows nothing else); AWS writes its own phase files next to it."""
    _check(source, lane)
    _batch_rules(source, batch_id)
    if batch_id is not None:
        return f"{lane}/{_day(day)}/{_name(batch_id, 'batch_id')}/01-input.jsonl"
    return f"{lane}/{_day(day)}/{_key(idempotency_key)}/01-input.json"
