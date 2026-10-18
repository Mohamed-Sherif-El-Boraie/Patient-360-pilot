# Core Domain, Contracts, Packages and Schema Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the pure, fully tested core of the Patient-360 pilot AI service: strict payload validation, the v3 processing logic (keys added/removed, classification, tier to lane), opaque ids, the public-output filter, the test-package data types and `ai.packages` output key (the selection rule is NOT part of this plan), and the normalised PostgreSQL schema.

**Architecture:** Plain Python functions over plain data in `src/patient360/domain/` with no I/O, so each one is testable with the example payloads in `tests/fixtures/flow`. The PostgreSQL schema is one migration file. Adapters (Blob, Bedrock, Postgres, HTTP) and pipelines are separate later plans.

**Tech Stack:** Python 3.13+ (suite passed on 3.13, 3.14 and 3.15.0b4; uses the builtin `frozendict` when present, `types.MappingProxyType` before 3.15), uv, Pydantic v2 (strict), pytest, ruff (lint), import-linter (domain may not import I/O libraries), PostgreSQL 16+.

**Spec:** `README.md` in this repository (sections 5 Storage, 7 API contracts, 8 Pipelines, 11 Validation policy). The payload examples are `prod/files/flow/**` and the package data is `prod/files/Test-Packages.xlsx`.

## Global Constraints

- Backend sends one visit JSON per call, never JSONL; batch JSONL is built by the AI service only.
- Strict validation: unknown keys are rejected (`extra="forbid"`), no type coercion, `source` must equal the endpoint.
- Validation errors never echo the input value (patient data must not reach logs).
- Renamed keys are final: `parameter_remark`, `patient_symptoms`, `descriptive_results`, `reference.range`, `critical.direction` in `critical_low|critical_high`.
- The Backend never sends `status`, `alert`, `catalogue`, `is_calculated`, `not_reported`, `catalogue_test_id`, `trend`, `value_raw`; the AI service adds `status`, `alert`, `lab_flag`, `critical.direction`.
- Tiers are `NORMAL | MILD | MODERATE | CRITICAL`; lanes are `normal` (NORMAL, MILD) and `critical` (MODERATE, CRITICAL).
- The public output removes `meta.model_id`, `meta.translation_model_id`, `meta.usage` and nothing else.
- Package selection rules (which packages, how many, order, scoring) are **not defined yet**: the owner will provide them. This plan contains no selection or scoring code.
- `record_id` = HMAC(patient_id, visit_id, endpoint, source_hash): the same data re-sent is the same record, changed data is a new record.
- Code stays small and plain: pure functions over plain data, no framework, no class hierarchy, no code without a caller or a test.
- Runtime target: Docker image from GitHub via ACR to App Service for Linux; Python 3.13+ now, 3.14 image fallback if a library lacks 3.15 wheels.
- JSON payloads live in `record_payloads`; `records` stays narrow; no circular foreign keys; no id arrays.
- No credentials, tokens or patient data in any file or log. Files are UTF-8.

## Review Focus

- A visit re-sent with changed data must not be dropped as a duplicate or crash on the primary key: `test_changed_data_or_endpoint_or_key_gives_a_new_id` (Task 6) and `test_changed_data_is_a_new_record_so_there_is_no_second_dedupe_rule` (Task 9).
- A ratio, titre or date in a result column (`1:64`, `2026-01-10`) must not be read as a number: `test_parse_numeric_reads_the_leading_number_like_v3` (Task 1).
- Absurdly deep JSON must be a 422 `json_invalid`, not a 500: `test_very_deep_nesting_is_invalid_not_a_crash` (Task 7).
- A visit with the same parameter twice must not break the lab_results insert: `test_repeated_parameters_and_the_package_cap_are_not_blocked_by_the_schema` (Task 9).
- A Backend payload carrying a logic key (for example `status`) must be rejected, not silently trusted: `test_unknown_key_is_rejected` in Task 6.
- A validation error on long symptom text must not leak that text: `test_errors_never_echo_the_input` in Task 6.
- An empty package list must still produce a valid `ai.packages: []`: `test_no_matches_gives_an_empty_list_and_creates_ai_if_missing` in Task 8.
- A package with no tests in the workbook (Profile 6283 style) must survive the import and never crash it: `test_forward_fill_keeps_a_package_without_tests` and `test_a_workbook_row_with_a_name_but_no_id_is_skipped_and_a_blank_name_is_empty_text` in Task 8.
- "Not reactive" and "Non-reactive" are negative (owner decision; v3 read "not reactive" as positive): `test_not_reactive_is_negative` in Task 2.

---

## File Structure

| Path | Responsibility |
|---|---|
| `pyproject.toml` | project, pytest config (`pythonpath=["src"]`) |
| `src/patient360/domain/compat.py` | `frozendict` on 3.15, `MappingProxyType` before |
| `src/patient360/domain/values.py` | `whole_or_text`, `parse_numeric`, `prune` |
| `src/patient360/domain/logic.py` | ranges, status, qualitative polarity, critical breach, alert (v3 port) |
| `src/patient360/domain/document.py` | `apply_logic(raw) -> LogicResult(doc, flat)` |
| `src/patient360/domain/classify.py` | `classify_patient`, `lane_for` |
| `src/patient360/domain/public.py` | `to_public` |
| `src/patient360/domain/models.py` | strict Pydantic models per endpoint |
| `src/patient360/domain/ids.py` | `source_hash`, `make_record_id`, `make_request_id` |
| `src/patient360/domain/validate.py` | `validate_payload(raw, endpoint) -> Valid | Invalid` |
| `src/patient360/domain/packages.py` | `Package`, `PackageMatch`, `attach_packages`, `forward_fill_packages` (no selection rule yet) |
| `migrations/001_init.sql` | the full normalised schema |
| `tests/` | one test file per module plus `fixtures/flow` (copy of `prod/files/flow`) |

Run all commands from `D:\IDH\Tasks\IDH-Smart-Testing\prod\v1\patient-360-pilot`.

---

### Task 1: Scaffold, example fixtures, value helpers

**Files:**
- Create: `pyproject.toml`
- Create: `uv.lock`
- Create: `src/patient360/__init__.py`
- Create: `src/patient360/domain/__init__.py`
- Create: `src/patient360/domain/compat.py`
- Create: `src/patient360/domain/values.py`
- Create: `tests/__init__.py`
- Create: `tests/domain/__init__.py`
- Create: `tests/conftest.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `whole_or_text(value) -> int | str | None`, `parse_numeric(value) -> int | float | None`, `prune(value) -> Any`; the `flow(rel_path)` fixture returning parsed JSON from `tests/fixtures/flow`.

- [ ] **Step 1: Write the failing tests**

`tests/conftest.py`:

```python
import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def flow():
    """Load a JSON file from the copied flow examples: flow("02-batch-critical/1-backend-input.json")."""
    def load(rel: str):
        return json.loads((FIXTURES / "flow" / rel).read_text(encoding="utf-8"))
    return load
```

`tests/domain/test_values.py`:

```python
from patient360.domain.values import parse_numeric, prune, whole_or_text


def test_whole_numbers_become_int_everything_else_stays_text():
    assert whole_or_text("177") == 177
    assert whole_or_text(" 200 ") == 200
    assert whole_or_text("-4") == -4
    assert whole_or_text("007") == "007"
    assert whole_or_text("5.3") == "5.3"
    assert whole_or_text("13.16") == "13.16"
    assert whole_or_text("Positive") == "Positive"
    assert whole_or_text(None) is None


def test_parse_numeric_reads_the_leading_number_like_v3():
    assert parse_numeric("13") == 13 and isinstance(parse_numeric("13"), int)
    assert parse_numeric("8.6") == 8.6
    assert parse_numeric("1.08  L") == 1.08
    assert parse_numeric("1,000") == 1000
    assert parse_numeric("<5") is None
    assert parse_numeric("1:64") is None and parse_numeric("5/10") is None      # titre, ratio
    assert parse_numeric("2026-01-10") is None and parse_numeric("5-10") is None  # date, range text
    assert parse_numeric("Positive") is None
    assert parse_numeric("") is None and parse_numeric(None) is None


def test_prune_drops_none_empty_and_empty_containers_but_keeps_zero_and_false():
    source = {"a": None, "b": "", "c": [], "d": {"e": None}, "f": 0, "g": False, "h": [1, None, ""]}
    assert prune(source) == {"f": 0, "g": False, "h": [1]}
    assert prune([None, ""]) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/domain/test_values.py -v`
Expected: FAIL with `ModuleNotFoundError` (or `ImportError`) for the module under test.

- [ ] **Step 3: Write the implementation**

`pyproject.toml`:

```toml
[project]
name = "patient360"
version = "0.1.0"
description = "Patient-360 pilot: AI service (contracts, domain logic, adapters)"
requires-python = ">=3.13"
dependencies = ["pydantic>=2.7"]

[dependency-groups]
dev = ["pytest>=8", "ruff>=0.6", "import-linter>=2.0"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/patient360"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]

[tool.ruff]
line-length = 120
target-version = "py313"
src = ["src", "tests"]

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP"]

[tool.importlinter]
root_package = "patient360"
include_external_packages = true

[[tool.importlinter.contracts]]
name = "domain stays pure: no I/O libraries"
type = "forbidden"
source_modules = ["patient360.domain"]
forbidden_modules = ["boto3", "botocore", "psycopg", "psycopg_pool", "azure", "fastapi", "httpx", "requests", "structlog"]
```

`src/patient360/domain/compat.py`:

```python
"""Python 3.15 `frozendict` with a fallback so the suite also runs on 3.13/3.14 (CI matrix)."""
from __future__ import annotations

try:  # Python 3.15+: frozendict is a builtin (PEP 814)
    from builtins import frozendict  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - exercised on older interpreters only
    from types import MappingProxyType as frozendict  # type: ignore[assignment]

__all__ = ["frozendict"]
```

`src/patient360/domain/values.py`:

```python
"""Small pure helpers shared by the domain logic (no I/O)."""
from __future__ import annotations

import math
import re
from typing import Any

_WHOLE = re.compile(r"^-?(?:0|[1-9]\d*)$")   # "007" stays text
_NOT_A_NUMBER_TAIL = re.compile(r"[:/]|-\d")  # "1:64" (titre), "5/10", "2026-01-10", "5-10"
_NUMBER_PREFIX = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


def whole_or_text(value: Any) -> int | str | None:
    """Whole numbers become ``int``; anything else stays text exactly as stored (trimmed)."""
    if value is None:
        return None
    text = str(value).strip()
    return int(text) if _WHOLE.match(text) else text


def parse_numeric(value: Any) -> int | float | None:
    """Port of v3 ``labValues.parseNumeric``: drop commas/spaces, read the leading number."""
    if value is None or value == "":
        return None
    text = re.sub(r"[,\s]", "", str(value))
    match = _NUMBER_PREFIX.match(text)
    if not match or _NOT_A_NUMBER_TAIL.match(text[match.end():]):
        return None
    number = float(match.group(0))
    if math.isnan(number) or math.isinf(number):
        return None
    return int(number) if number.is_integer() else number


def prune(value: Any) -> Any:
    """Drop ``None``, ``""`` and empty containers recursively. Keeps ``0`` and ``False``."""
    if isinstance(value, list):
        items = [kept for kept in (prune(item) for item in value) if kept is not None]
        return items or None
    if isinstance(value, dict):
        out = {key: kept for key, item in value.items() if (kept := prune(item)) is not None}
        return out or None
    if value is None or value == "":
        return None
    return value
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/domain/test_values.py -v`
Expected: PASS (all tests in the listed files).

Before Step 1, create the empty package files and copy the example payloads:

```powershell
New-Item -ItemType Directory -Force src\patient360\domain, tests\domain, tests\fixtures | Out-Null
New-Item -ItemType File -Force src\patient360\__init__.py, src\patient360\domain\__init__.py, tests\__init__.py, tests\domain\__init__.py | Out-Null
Copy-Item ..\..\files\flow tests\fixtures\flow -Recurse -Force
uv sync
```

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock src/patient360/__init__.py src/patient360/domain/__init__.py src/patient360/domain/compat.py src/patient360/domain/values.py tests/__init__.py tests/domain/__init__.py tests/conftest.py tests/domain/test_values.py
git commit -m "build: scaffold project, fixtures and value helpers"
```

---

### Task 2: Processing logic port from v3 (ranges, status, qualitative, critical, alert)

**Files:**
- Create: `src/patient360/domain/logic.py`

**Interfaces:**
- Consumes: Task 1 `parse_numeric`.
- Produces: `reference_bounds`, `parse_text_range`, `classify_result`, `classify_qualitative`, `lab_flag_status`, `critical_breach(value, bounds, *, status, ref_low, ref_high)`, `alert_for(status, direction)`, `clean_bound`.

- [ ] **Step 1: Write the failing tests**

`tests/domain/test_logic.py`:

```python
import pytest

from patient360.domain.logic import (
    alert_for,
    classify_qualitative,
    classify_result,
    critical_breach,
    lab_flag_status,
    parse_text_range,
    reference_bounds,
)


@pytest.mark.parametrize("value,low,high,expected", [
    ("177", "13", "43", "high"),
    ("5.3", "8.6", "10.2", "low"),
    ("43", "13", "43", "normal"),      # exactly on a bound is normal
    ("13", "13", "43", "normal"),
    ("5", "0", "0", None),             # 0/0 means "no range recorded"
    ("500", "-1", "9999", None),       # -1/9999 means "no limit"
    ("5", None, "10", "normal"),       # one-sided range the value complies with
    ("12", None, "10", "high"),
    ("Positive", "1", "2", None),      # not a number
])
def test_classify_result(value, low, high, expected):
    assert classify_result(value, low, high) == expected


def test_reference_bounds_treats_lims_markers_as_no_range():
    assert reference_bounds("0", "0") == (None, None)
    assert reference_bounds("-1", "9999") == (None, None)
    assert reference_bounds("13", "43") == (13, 43)


@pytest.mark.parametrize("text,expected", [
    ("13 - 43", (13, 43)),
    ("6.7 to 22.4", (6.7, 22.4)),
    ("< 5.7", (None, 5.7)),
    (">= 2.0", (2, None)),
    ("up to 30", (None, 30)),
    ("43 - 13", (13, 43)),             # a reversed pair is a data-entry artefact
    ("Negative", (None, None)),
    ("1:64", (None, None)),            # a titre is not a range
    ("", (None, None)),
])
def test_parse_text_range(text, expected):
    assert parse_text_range(text) == expected


@pytest.mark.parametrize("value,expected_text,expected", [
    ("Negative", None, "normal"),
    ("Target not Detected", None, "normal"),
    ("Negative for all tested mutations and deletions", None, "normal"),
    ("Not Detected", None, "normal"),
    ("Non detected", None, "normal"),
    ("Positive", None, "abnormal"),
    ("Weak Positive", None, "abnormal"),
    ("Positive at Heterozygous state", None, "abnormal"),
    ("Nil", "Nil", "normal"),          # equals the expected text
    ("Nil", None, None),
    ("Yellow", None, None),
    ("Normal Trace", None, None),
    ("Negative and positive", None, None),   # conflicting wording stays unjudged
])
def test_classify_qualitative(value, expected_text, expected):
    assert classify_qualitative(value, expected_text) == expected


def test_not_reactive_is_negative():
    # v3 read it as positive because it contains "reactive"; the owner decided it is negative.
    assert classify_qualitative("Not reactive", None) == "normal"
    assert classify_qualitative("Non-reactive", None) == "normal"
    assert classify_qualitative("Reactive", None) == "abnormal"


def test_lab_flag_status():
    assert lab_flag_status("H") == "high" and lab_flag_status("l") == "low"
    assert lab_flag_status(None) is None and lab_flag_status("N") is None


@pytest.mark.parametrize("value,bounds,status,ref,expected", [
    ("5.3", {"low": 6.5, "high": 13}, "low", (8.6, 10.2), "low"),
    ("13.67", {"low": None, "high": 6}, "high", (0.7, 1.3), "high"),
    ("0.9", {"low": None, "high": 6}, "normal", (0.6, 1.1), None),
    ("5.9", {"low": None, "high": 6}, "high", (0.7, 1.3), None),
    ("6", {"low": None, "high": 6}, "high", (0.7, 1.3), "high"),      # inclusive
    ("5", {"low": None, "high": 6}, "low", (0.7, 1.3), None),         # a LOW value is never tested against HIGH
    ("500", {"low": None, "high": 400}, None, (None, None), "high"),  # no range: both sides are tested
    ("Positive", {"low": 1, "high": 2}, "abnormal", (None, None), None),
])
def test_critical_breach(value, bounds, status, ref, expected):
    assert critical_breach(value, bounds, status=status, ref_low=ref[0], ref_high=ref[1]) == expected


def test_normal_status_ignores_an_incoherent_critical_low():
    # critical low 500 against a 2.0-7.0 range is a unit mismatch (the v3 neutrophil bug)
    assert critical_breach("4.28", {"low": 500, "high": None}, status="normal", ref_low=2.0, ref_high=7.0) is None


def test_alert_for():
    assert alert_for("low", "low") == "CRITICAL"
    assert alert_for("high", None) == "MEDICAL_ATTENTION"
    assert alert_for("abnormal", None) == "MEDICAL_ATTENTION"
    assert alert_for("normal", None) == "NORMAL"
    assert alert_for(None, None) == "NOT_CLASSIFIED"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/domain/test_logic.py -v`
Expected: FAIL with `ModuleNotFoundError` (or `ImportError`) for the module under test.

- [ ] **Step 3: Write the implementation**

`src/patient360/domain/logic.py`:

```python
"""Status, alert and critical-direction rules. Pure port of v3 ``labValues.js`` / ``resultClassification``.

Rule order for a result's status (first answer wins):
  1. the numeric range (CONVLOW/CONVHIGH -> NORMAL*VALUE -> numbers read from the range text)
  2. the wording of a qualitative result (negative / positive vocabulary)
  3. the laboratory's own flag (H -> high, L -> low); otherwise None = "not judged"
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from patient360.domain.values import parse_numeric

ABNORMAL = frozenset({"high", "low", "abnormal"})
NOT_SET_PAIRS = ((0, 0), (-1, 9999))  # LIMS markers for "no range recorded"
SENTINEL_BOUNDS = frozenset({-1, 9999})  # "no limit" markers in critical columns

_MUTATION = r"(?:mutation|mutations|deletion|deletions|variant|variants)"
NEGATIVE_PATTERNS = (
    re.compile(r"\bnegative\b"),
    re.compile(r"\b(?:not|non)\s+detected\b"),
    re.compile(r"\bnone\s+detected\b"),
    re.compile(r"\b(?:non[\s-]?|not\s+)reactive\b"),
    re.compile(rf"\bno\s+(?:evidence\s+of\s+)?{_MUTATION}\b"),
)
# Negative clauses are removed before the positive check, so "not detected" is not read as "detected".
_NEGATIVE_CLAUSES = (
    re.compile(r"\bno\s+(?:[a-z0-9/-]+\s+){0,8}(?:detected|found|identified|present)\b"),
    re.compile(r"\b(?:not|non)\s+detected\b"),
    re.compile(r"\bnone\s+detected\b"),
    re.compile(r"\b(?:non[\s-]?|not\s+)reactive\b"),
    re.compile(rf"\bno\s+(?:evidence\s+of\s+)?{_MUTATION}\b"),
    re.compile(r"\bnegative\b"),
)
POSITIVE_PATTERN = re.compile(
    rf"\bpositive\b|\breactive\b|\bdetected\b|\b{_MUTATION}\s+(?:found|identified|present)\b"
)

_TWO_SIDED = re.compile(r"^(-?\d+(?:\.\d+)?)\s*(?:-|to)\s*(-?\d+(?:\.\d+)?)$", re.I)
_UPPER_ONLY = re.compile(r"^(?:<=?|≤|up to|less than|below)\s*(-?\d+(?:\.\d+)?)$", re.I)
_LOWER_ONLY = re.compile(r"^(?:>=?|≥|more than|above|over)\s*(-?\d+(?:\.\d+)?)$", re.I)
_DASHES = re.compile("[‐-―−]")


def reference_bounds(low: Any, high: Any) -> tuple[int | float | None, int | float | None]:
    """Numeric bounds, or (None, None) when the pair is a LIMS "not set" marker."""
    num_low, num_high = parse_numeric(low), parse_numeric(high)
    if (num_low, num_high) in NOT_SET_PAIRS:
        return None, None
    return num_low, num_high


def parse_text_range(text: Any) -> tuple[int | float | None, int | float | None]:
    """Read a range out of text such as ``6.7 - 22.4``, ``< 5.7`` or ``up to 30``; nothing else."""
    if text is None:
        return None, None
    t = _DASHES.sub("-", str(text).strip())
    t = re.sub(r"\s+", " ", t)
    if not t:
        return None, None
    if m := _TWO_SIDED.match(t):
        low, high = float(m.group(1)), float(m.group(2))
        low, high = (low, high) if low <= high else (high, low)
        return parse_numeric(low), parse_numeric(high)
    if m := _UPPER_ONLY.match(t):
        return None, parse_numeric(m.group(1))
    if m := _LOWER_ONLY.match(t):
        return parse_numeric(m.group(1)), None
    return None, None


def classify_result(value: Any, low: Any, high: Any) -> str | None:
    """``low`` / ``high`` / ``normal`` from arithmetic, or None. A bound that is None is never compared."""
    number = parse_numeric(value)
    if number is None:
        return None
    lo, hi = reference_bounds(low, high)
    if lo is not None and number < lo:
        return "low"
    if hi is not None and number > hi:
        return "high"
    if lo is not None or hi is not None:
        return "normal"
    return None


def normalise_text(text: Any) -> str:
    return " ".join(str(text if text is not None else "").split()).lower()


def polarity(text: Any) -> str | None:
    """``negative`` / ``positive`` / ``conflicting`` / None for a word result."""
    value = normalise_text(text)
    if not value:
        return None
    has_negative = any(p.search(value) for p in NEGATIVE_PATTERNS)
    remainder = value
    for clause in _NEGATIVE_CLAUSES:
        remainder = clause.sub(" ", remainder)
    has_positive = bool(POSITIVE_PATTERN.search(remainder))
    if has_negative and has_positive:
        return "conflicting"
    if has_negative:
        return "negative"
    if has_positive:
        return "positive"
    return None


def classify_qualitative(value: Any, expected_text: Any) -> str | None:
    """``normal`` / ``abnormal`` for a word result. ``abnormal`` has NO direction."""
    result = normalise_text(value)
    if not result:
        return None
    kind = polarity(result)
    if kind == "conflicting":
        return None
    if kind == "negative":
        return "normal"
    if kind == "positive":
        return "abnormal"
    expected = normalise_text(expected_text)
    return "normal" if expected and result == expected else None


def lab_flag_status(flag: Any) -> str | None:
    return {"H": "high", "L": "low"}.get(str(flag or "").strip().upper())


def clean_bound(value: Any, *, is_low: bool) -> int | float | None:
    """A critical bound, or None for the -1 / 9999 placeholders (and any negative critical LOW)."""
    number = parse_numeric(value)
    if number is None or number in SENTINEL_BOUNDS:
        return None
    if is_low and number < 0:
        return None
    return number


def _coherent_sides(bounds: Mapping[str, Any], ref_low: Any, ref_high: Any) -> tuple[bool, bool]:
    low, high = bounds.get("low"), bounds.get("high")
    low_ok = low is not None and ref_high is not None and low <= ref_high
    high_ok = high is not None and ref_low is not None and high >= ref_low
    return low_ok, high_ok


def critical_breach(
    value: Any, bounds: Mapping[str, Any] | None, *, status: str | None,
    ref_low: Any = None, ref_high: Any = None,
) -> str | None:
    """``low`` / ``high`` / None. Direction-aware, inclusive on both bounds.

    A value already known to be HIGH is only compared with the critical HIGH bound (and LOW with LOW).
    ``normal`` is checked only on sides whose bound is coherent with the reference range.
    No status (no usable range) and ``abnormal`` are checked on both sides.
    """
    number = parse_numeric(value)
    if number is None or not bounds:
        return None
    if status == "normal":
        check_low, check_high = _coherent_sides(bounds, ref_low, ref_high)
    else:
        check_low = status in ("low", "abnormal", None)
        check_high = status in ("high", "abnormal", None)
    low, high = bounds.get("low"), bounds.get("high")
    if check_low and low is not None and number <= low:
        return "low"
    if check_high and high is not None and number >= high:
        return "high"
    return None


def alert_for(status: str | None, direction: str | None) -> str:
    if direction:
        return "CRITICAL"
    if status in ABNORMAL:
        return "MEDICAL_ATTENTION"
    if status == "normal":
        return "NORMAL"
    return "NOT_CLASSIFIED"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/domain/test_logic.py -v`
Expected: PASS (all tests in the listed files).

- [ ] **Step 5: Commit**

```bash
git add src/patient360/domain/logic.py tests/domain/test_logic.py
git commit -m "feat: port v3 result classification and critical logic"
```

---

### Task 3: Document builder (keys removed and added) with golden tests

**Files:**
- Create: `src/patient360/domain/document.py`

**Interfaces:**
- Consumes: Tasks 1-2 functions; fixtures `*/1-backend-input.json` and `*/2a-ai-service-document-before-rag.json`.
- Produces: `LogicResult(doc: dict, flat: list[dict])` and `apply_logic(raw: Mapping) -> LogicResult`. `doc` is the AI document before RAG; `flat` is one row per parameter for classification.

- [ ] **Step 1: Write the failing tests**

`tests/domain/test_document_golden.py`:

```python
import copy

import pytest

from patient360.domain.document import apply_logic

CASES = ["01-batch-normal", "02-batch-critical", "03-urgent", "04-on-demand", "06-descriptive"]
FORBIDDEN = {"patient_id", "visit_id", "company_name", "lab_remark", "symptoms", "trend", "value_raw", "catalogue"}


def _keys(node, found=None):
    found = set() if found is None else found
    if isinstance(node, dict):
        for key, child in node.items():
            found.add(key)
            _keys(child, found)
    elif isinstance(node, list):
        for child in node:
            _keys(child, found)
    return found


@pytest.mark.parametrize("case", CASES)
def test_document_equals_the_flow_2a_example(flow, case):
    raw = flow(f"{case}/1-backend-input.json")
    expected = flow(f"{case}/2a-ai-service-document-before-rag.json")
    assert apply_logic(raw).doc == expected


@pytest.mark.parametrize("case", CASES)
def test_document_has_no_ids_company_or_retired_keys(flow, case):
    doc = apply_logic(flow(f"{case}/1-backend-input.json")).doc
    assert not (_keys(doc) & FORBIDDEN)


def test_input_is_never_mutated(flow):
    raw = flow("02-batch-critical/1-backend-input.json")
    before = copy.deepcopy(raw)
    apply_logic(raw)
    assert raw == before


def test_critical_patient_rows(flow):
    doc = apply_logic(flow("02-batch-critical/1-backend-input.json")).doc
    by_name = {r["parameter_name"]: r for t in doc["tests"] for r in t["results"]}
    calcium = by_name["Calcium"]
    assert (calcium["status"], calcium["alert"], calcium["lab_flag"]) == ("low", "CRITICAL", "Low")
    assert calcium["critical"]["direction"] == "critical_low" and calcium["value"] == "5.3"
    assert by_name["Blood Urea"]["value"] == 177 and by_name["Blood Urea"]["alert"] == "MEDICAL_ATTENTION"
    assert by_name["CREATININE (serum)"]["critical"]["direction"] == "critical_high"
    assert by_name["Glucose After 2 Hours"]["history"] == [
        {"date": "2026-01-10", "value": 241}, {"date": "2026-05-02", "value": "205.5"}]


def test_normal_patient_has_a_critical_limit_but_no_direction(flow):
    doc = apply_logic(flow("01-batch-normal/1-backend-input.json")).doc
    creatinine = next(r for t in doc["tests"] for r in t["results"] if r["parameter_name"] == "CREATININE (serum)")
    assert creatinine["alert"] == "NORMAL" and "direction" not in creatinine["critical"]
```

`tests/domain/test_document_text_range.py`:

```python
from patient360.domain.document import apply_logic


def doc_row(range_text):
    raw = {"source": "urgent", "patient": {"age_years": 30, "sex": "male"}, "tests": [{"test_name": "T", "results": [
        {"parameter_name": "P", "value": "5", "reference": {"range": range_text}}]}]}
    return apply_logic(raw).doc["tests"][0]["results"][0]


def test_a_not_set_text_range_does_not_become_bounds():
    for marker in ("-1 - 9999", "0 - 0"):
        row = doc_row(marker)
        assert "low" not in row.get("reference", {}) and "high" not in row.get("reference", {})
        assert "status" not in row


def test_a_real_text_range_still_classifies():
    assert doc_row("1 - 4")["status"] == "high"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/domain/test_document_golden.py tests/domain/test_document_text_range.py -v`
Expected: FAIL with `ModuleNotFoundError` (or `ImportError`) for the module under test.

- [ ] **Step 3: Write the implementation**

`src/patient360/domain/document.py`:

```python
"""Backend payload -> AI document (flow "2a", before RAG). Pure: no I/O, never mutates its input."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from patient360.domain.logic import (
    alert_for,
    classify_qualitative,
    classify_result,
    clean_bound,
    critical_breach,
    lab_flag_status,
    parse_text_range,
    reference_bounds,
)
from patient360.domain.values import prune, whole_or_text

LAB_FLAG_WORDS = {"H": "High", "L": "Low"}


@dataclass(frozen=True)
class LogicResult:
    doc: dict[str, Any]          # what goes to AWS (before RAG): keys added/removed, nulls pruned
    flat: list[dict[str, Any]]   # one row per parameter, for classify_patient and the lab_results table


def _result(raw: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    ref = raw.get("reference") or {}
    value = raw.get("value")
    low, high = reference_bounds(ref.get("low"), ref.get("high"))
    if low is None and high is None:
        low, high = reference_bounds(*parse_text_range(ref.get("range")))
    status = (
        classify_result(value, low, high)
        or classify_qualitative(value, ref.get("range"))
        or lab_flag_status(raw.get("lab_flag"))
    )
    critical = raw.get("critical")
    direction = None
    if critical:
        bounds = {
            "low": clean_bound(critical.get("low"), is_low=True),
            "high": clean_bound(critical.get("high"), is_low=False),
        }
        direction = critical_breach(value, bounds, status=status, ref_low=low, ref_high=high)
    out = {
        "parameter_name": raw.get("parameter_name"),
        "result_date": raw.get("result_date"),
        "value": whole_or_text(value),
        "unit": raw.get("unit"),
        "reference": {"low": low, "high": high, "range": whole_or_text(ref.get("range"))},
        "status": status,
        "lab_flag": LAB_FLAG_WORDS.get(str(raw.get("lab_flag") or "").strip().upper()),
        "alert": alert_for(status, direction),
        "critical": (
            {
                "low": critical.get("low"), "high": critical.get("high"),
                "range_text": critical.get("range_text"),
                "direction": f"critical_{direction}" if direction else None,
            }
            if critical else None
        ),
        "previous_value": whole_or_text(raw.get("previous_value")),
        "previous_date": raw.get("previous_date"),
        "parameter_remark": raw.get("parameter_remark"),
        "history": [
            {"date": h.get("date"), "value": whole_or_text(h.get("value"))}
            for h in (raw.get("history") or [])
        ],
    }
    flat = {
        "parameter_name": raw.get("parameter_name"), "value": value,
        "status": status, "is_critical": bool(direction),
    }
    return out, flat


def apply_logic(raw: Mapping[str, Any]) -> LogicResult:
    """Remove keys (ids, company name, nulls), add keys (status, alert, direction, mapped flag...)."""
    flat: list[dict[str, Any]] = []
    tests = []
    for test in raw.get("tests") or []:
        results = []
        for item in test.get("results") or []:
            out, row = _result(item)
            results.append(out)
            flat.append(row)
        tests.append({
            "test_name": test.get("test_name"),
            "test_remark": test.get("test_remark"),
            "results": results,
        })
    doc = prune({
        "source": raw.get("source"),
        "patient": raw.get("patient"),
        "tests": tests,
        "descriptive_results": raw.get("descriptive_results"),
        "visitAnswerList": raw.get("visitAnswerList"),
    }) or {}
    return LogicResult(doc=doc, flat=flat)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/domain/test_document_golden.py tests/domain/test_document_text_range.py -v`
Expected: PASS (all tests in the listed files).

- [ ] **Step 5: Commit**

```bash
git add src/patient360/domain/document.py tests/domain/test_document_golden.py tests/domain/test_document_text_range.py
git commit -m "feat: build AI document from backend input"
```

---

### Task 4: Patient tier and lane

**Files:**
- Create: `src/patient360/domain/classify.py`

**Interfaces:**
- Consumes: Task 3 `LogicResult.flat`.
- Produces: `classify_patient(flat) -> 'NORMAL'|'MILD'|'MODERATE'|'CRITICAL'`, `lane_for(tier) -> 'normal'|'critical'`, `LANE_BY_TIER`.

- [ ] **Step 1: Write the failing tests**

`tests/domain/test_classify.py`:

```python
import pytest

from patient360.domain.classify import LANE_BY_TIER, classify_patient, lane_for
from patient360.domain.document import apply_logic

EXPECTED = {
    "01-batch-normal": "NORMAL", "02-batch-critical": "CRITICAL", "03-urgent": "MILD",
    "04-on-demand": "CRITICAL", "06-descriptive": "MODERATE",
}


@pytest.mark.parametrize("case,tier", EXPECTED.items())
def test_tier_matches_the_flow_output(flow, case, tier):
    flat = apply_logic(flow(f"{case}/1-backend-input.json")).flat
    assert classify_patient(flat) == tier
    assert flow(f"{case}/5-ai-service-output.json")["tier"] == tier


def row(status, *, critical=False, value="1"):
    return {"value": value, "status": status, "is_critical": critical}


def test_precedence_critical_then_unrangeable_then_three_abnormal_then_mild_then_normal():
    assert classify_patient([row("high", critical=True), row(None)]) == "CRITICAL"
    assert classify_patient([row(None), row("normal")]) == "MODERATE"                    # unrangeable: fail safe
    assert classify_patient([row("high"), row("low"), row("abnormal")]) == "MODERATE"    # 3+ abnormal
    assert classify_patient([row("high"), row("low")]) == "MILD"
    assert classify_patient([row("normal"), row("normal")]) == "NORMAL"


def test_rows_without_a_result_are_ignored_and_no_data_escalates():
    assert classify_patient([row(None, value=""), row("normal")]) == "NORMAL"
    assert classify_patient([]) == "MODERATE"


def test_lanes():
    tiers = ("NORMAL", "MILD", "MODERATE", "CRITICAL")
    assert [lane_for(t) for t in tiers] == ["normal", "normal", "critical", "critical"]
    assert set(LANE_BY_TIER) == {"NORMAL", "MILD", "MODERATE", "CRITICAL"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/domain/test_classify.py -v`
Expected: FAIL with `ModuleNotFoundError` (or `ImportError`) for the module under test.

- [ ] **Step 3: Write the implementation**

`src/patient360/domain/classify.py`:

```python
"""Patient tier and model lane. Pure port of v3 ``severity.decideTier`` + ``modelRouter``."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from patient360.domain.compat import frozendict
from patient360.domain.logic import ABNORMAL

# normal = Haiku lane, critical = Sonnet lane (BEDROCK_LARGE_MODEL_TIERS default in v3 = MODERATE,CRITICAL)
LANE_BY_TIER = frozendict({"NORMAL": "normal", "MILD": "normal", "MODERATE": "critical", "CRITICAL": "critical"})


def _has_result(row: Mapping[str, Any]) -> bool:
    return str(row.get("value") if row.get("value") is not None else "").strip() != ""


def classify_patient(flat: Sequence[Mapping[str, Any]]) -> str:
    """First matching rule wins: critical > unrangeable (fail safe) > 3+ abnormal > 1-2 abnormal > normal."""
    rows = [row for row in flat if _has_result(row)]
    if not rows:  # no data is not "no findings": escalate rather than call it normal
        return "MODERATE"
    if any(row["is_critical"] for row in rows):
        return "CRITICAL"
    if any(row["status"] is None for row in rows):
        return "MODERATE"
    abnormal = sum(1 for row in rows if row["status"] in ABNORMAL)
    if abnormal >= 3:
        return "MODERATE"
    return "MILD" if abnormal else "NORMAL"


def lane_for(tier: str) -> str:
    return LANE_BY_TIER[tier]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/domain/test_classify.py -v`
Expected: PASS (all tests in the listed files).

- [ ] **Step 5: Commit**

```bash
git add src/patient360/domain/classify.py tests/domain/test_classify.py
git commit -m "feat: classify patient tier and lane"
```

---

### Task 5: Public output filter

**Files:**
- Create: `src/patient360/domain/public.py`

**Interfaces:**
- Consumes: fixtures `*/5-ai-service-output.json`.
- Produces: `to_public(record: Mapping) -> dict` (deep copy without model ids and usage).

- [ ] **Step 1: Write the failing tests**

`tests/domain/test_public.py`:

```python
import copy

import pytest

from patient360.domain.public import to_public

CASES = ["01-batch-normal", "02-batch-critical", "03-urgent", "04-on-demand"]


@pytest.mark.parametrize("case", CASES)
def test_public_output_drops_model_ids_and_usage_and_keeps_the_rest(flow, case):
    internal = flow(f"{case}/5-ai-service-output.json")
    public = to_public(internal)
    assert not ({"model_id", "translation_model_id", "usage"} & set(public["meta"]))
    assert public["meta"]["generated_at"] == internal["meta"]["generated_at"]
    assert {k: v for k, v in public.items() if k != "meta"} == {k: v for k, v in internal.items() if k != "meta"}


def test_input_is_not_mutated(flow):
    internal = flow("02-batch-critical/5-ai-service-output.json")
    before = copy.deepcopy(internal)
    to_public(internal)
    assert internal == before


def test_a_record_without_meta_is_returned_unchanged():
    assert to_public({"status": "error"}) == {"status": "error"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/domain/test_public.py -v`
Expected: FAIL with `ModuleNotFoundError` (or `ImportError`) for the module under test.

- [ ] **Step 3: Write the implementation**

`src/patient360/domain/public.py`:

```python
"""Internal AI output -> public output for the Backend (no model ids, no tokens, no cost)."""
from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

PRIVATE_PATHS = ("meta.model_id", "meta.translation_model_id", "meta.usage")


def to_public(record: Mapping[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(dict(record))
    for path in PRIVATE_PATHS:
        *parents, leaf = path.split(".")
        node: Any = out
        for key in parents:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, dict):
            node.pop(leaf, None)
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/domain/test_public.py -v`
Expected: PASS (all tests in the listed files).

- [ ] **Step 5: Commit**

```bash
git add src/patient360/domain/public.py tests/domain/test_public.py
git commit -m "feat: strip internal model fields from public output"
```

---

### Task 6: Opaque record ids

**Files:**
- Create: `src/patient360/domain/ids.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `source_hash(payload: Mapping) -> str`, `make_record_id(key: bytes, *, patient_id, visit_id, endpoint, source_hash) -> str` (32 hex chars, also the Backend idempotency key), `make_request_id() -> str`.

- [ ] **Step 1: Write the failing tests**

`tests/domain/test_ids.py`:

```python
from patient360.domain.ids import make_record_id, make_request_id, source_hash

KEY = b"test-key"
BASE = {"patient_id": 7, "visit_id": 9, "endpoint": "urgent"}


def record_id(**changes):
    args = {**BASE, "source_hash": "h1", **changes}
    return make_record_id(KEY, **args)


def test_same_input_same_id_and_it_is_opaque():
    rid = record_id()
    assert rid == record_id() and len(rid) == 32
    assert "7" not in rid[:0] and "|" not in rid


def test_changed_data_or_endpoint_or_key_gives_a_new_id():
    assert record_id(source_hash="h2") != record_id()      # re-sent visit with changed data is not a duplicate
    assert record_id(endpoint="batch") != record_id()
    assert make_record_id(b"other", **BASE, source_hash="h1") != record_id()


def test_source_hash_ignores_key_order_and_whitespace_but_not_values():
    assert source_hash({"a": 1, "b": [1, 2]}) == source_hash({"b": [1, 2], "a": 1})
    assert source_hash({"a": 1}) != source_hash({"a": 2})


def test_request_ids_are_unique():
    assert make_request_id() != make_request_id()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/domain/test_ids.py -v`
Expected: FAIL with `ModuleNotFoundError` (or `ImportError`) for the module under test.

- [ ] **Step 3: Write the implementation**

`src/patient360/domain/ids.py`:

```python
"""Opaque ids. A re-send with changed data is a NEW record; an identical re-send is the same record."""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Mapping
from typing import Any


def source_hash(payload: Mapping[str, Any]) -> str:
    """Hash of the content, not of the bytes: whitespace and key order do not make a new version."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def make_record_id(key: bytes, *, patient_id: int, visit_id: int, endpoint: str, source_hash: str) -> str:
    """HMAC (not plain SHA-256: short numeric ids can be guessed). Also the Backend idempotency key."""
    message = f"{patient_id}|{visit_id}|{endpoint}|{source_hash}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:32]


def make_request_id() -> str:
    """Symptom-checker calls are many per visit, so each gets a random id."""
    return uuid.uuid4().hex
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/domain/test_ids.py -v`
Expected: PASS (all tests in the listed files).

- [ ] **Step 5: Commit**

```bash
git add src/patient360/domain/ids.py tests/domain/test_ids.py
git commit -m "feat: opaque record ids that change when the data changes"
```

---

### Task 7: Strict models and payload validator

**Files:**
- Create: `src/patient360/domain/models.py`
- Create: `src/patient360/domain/validate.py`

**Interfaces:**
- Consumes: Task 1 `frozendict`; fixtures `*/1-backend-input.json`.
- Produces: `validate_payload(raw: bytes, endpoint: str) -> Valid | Invalid`; `Valid(model)`, `Invalid(errors: list[{loc, msg, type}])`; error types `json_invalid`, `unknown_endpoint`, `source_mismatch` plus Pydantic's.

- [ ] **Step 1: Write the failing tests**

`tests/domain/test_models.py`:

```python
import copy
import json

import pytest

from patient360.domain.validate import Invalid, Valid, validate_payload

VISIT_CASES = [("01-batch-normal", "batch"), ("02-batch-critical", "batch"), ("03-urgent", "urgent"),
               ("04-on-demand", "on-demand"), ("06-descriptive", "on-demand")]


def raw(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


@pytest.mark.parametrize("case,endpoint", VISIT_CASES)
def test_every_flow_visit_input_is_valid(flow, case, endpoint):
    assert isinstance(validate_payload(raw(flow(f"{case}/1-backend-input.json")), endpoint), Valid)


@pytest.mark.parametrize("name", ["05-symptom-checker/1-backend-input.json",
                                  "05b-symptom-checker-guardrail-blocked/1-backend-input.json"])
def test_symptom_checker_inputs_are_valid(flow, name):
    assert isinstance(validate_payload(raw(flow(name)), "symptom-checker"), Valid)


def test_unknown_key_is_rejected(flow):
    body = flow("03-urgent/1-backend-input.json")
    body["status"] = "high"                      # logic keys must never come from the Backend
    result = validate_payload(raw(body), "urgent")
    assert isinstance(result, Invalid) and result.errors[0]["loc"] == ["status"]


def test_source_must_equal_the_endpoint(flow):
    result = validate_payload(raw(flow("03-urgent/1-backend-input.json")), "batch")
    assert isinstance(result, Invalid) and result.errors[0]["type"] == "source_mismatch"


def test_strict_types_reject_a_string_visit_id(flow):
    body = flow("03-urgent/1-backend-input.json")
    body["visit_id"] = "2070000003"
    assert isinstance(validate_payload(raw(body), "urgent"), Invalid)


def test_symptom_text_is_limited_to_1000_characters(flow):
    body = copy.deepcopy(flow("05-symptom-checker/1-backend-input.json"))
    body["patient_symptoms"] = "x" * 1001
    assert isinstance(validate_payload(raw(body), "symptom-checker"), Invalid)


def test_very_deep_nesting_is_invalid_not_a_crash():
    assert validate_payload(b"[" * 200000, "urgent").errors[0]["type"] == "json_invalid"


def test_unknown_key_names_are_cut_short_in_the_error(flow):
    body = flow("03-urgent/1-backend-input.json")
    body["k" * 500] = 1
    loc = validate_payload(raw(body), "urgent").errors[0]["loc"]
    assert len(loc[0]) == 60


def test_broken_json_and_unknown_endpoint():
    assert validate_payload(b'{"a": ', "urgent").errors[0]["type"] == "json_invalid"
    assert validate_payload(b"{}", "nope").errors[0]["type"] == "unknown_endpoint"


def test_errors_never_echo_the_input(flow):
    body = flow("05-symptom-checker/1-backend-input.json")
    body["patient_symptoms"] = "SECRET-SYMPTOM-TEXT " * 100   # too long -> an error that could echo the value
    result = validate_payload(raw(body), "symptom-checker")
    assert isinstance(result, Invalid) and "SECRET-SYMPTOM-TEXT" not in json.dumps(result.errors)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/domain/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError` (or `ImportError`) for the module under test.

- [ ] **Step 3: Write the implementation**

`src/patient360/domain/models.py`:

```python
"""Strict payload models = the Backend -> AI contract (see the flow files). Unknown keys are rejected."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Date = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]
Number = int | float


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Reference(Strict):
    low: str | None
    high: str | None
    range: str | None


class Critical(Strict):
    low: Number | None
    high: Number | None
    range_text: str | None


class HistoryPoint(Strict):
    date: Date
    value: str | None


class Result(Strict):
    parameter_name: str
    result_date: Date | None
    value: str | None
    unit: str | None
    reference: Reference
    lab_flag: Literal["H", "L"] | None
    critical: Critical | None
    previous_value: str | None
    previous_date: Date | None
    parameter_remark: str | None
    history: list[HistoryPoint] | None


class Test(Strict):
    test_name: str
    test_remark: str | None
    results: list[Result]


class DescriptiveResult(Strict):
    test_name: str
    result_date: Date | None
    text: str


class PreparationAnswer(Strict):
    PreparationID: int
    PreparationName: str
    Answer: str


class Patient(Strict):
    age_years: Annotated[int, Field(ge=0, le=130)]
    sex: Literal["male", "female"]


class VisitPayload(Strict):
    """Used by /v1/urgent, /v1/on-demand and /v1/batch (one visit per call)."""
    source: Literal["batch", "urgent", "on-demand"]
    patient_id: int
    visit_id: int
    company_name: str | None
    patient: Patient
    tests: list[Test]
    descriptive_results: list[DescriptiveResult]
    visitAnswerList: list[PreparationAnswer]


class SymptomPayload(Strict):
    """/v1/symptom-checker: the visit is already stored, so only 4 keys arrive."""
    source: Literal["symptom-checker"]
    patient_id: int
    visit_id: int
    patient_symptoms: Annotated[str, StringConstraints(min_length=1, max_length=1000)]
```

`src/patient360/domain/validate.py`:

```python
"""Trust boundary: bytes in -> Valid | Invalid. Strict parse only; repair is an optional adapter (later plan)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from patient360.domain.compat import frozendict
from patient360.domain.models import SymptomPayload, VisitPayload

MAX_LOC_PART = 60   # an unknown key name is sender-chosen text; keep what we store and return short

MODELS = frozendict({
    "urgent": VisitPayload,
    "on-demand": VisitPayload,
    "batch": VisitPayload,
    "symptom-checker": SymptomPayload,
})


@dataclass(frozen=True)
class Valid:
    payload: BaseModel


@dataclass(frozen=True)
class Invalid:
    # Never contains the input value: errors are saved and returned (422), and the input is patient data.
    errors: list[dict[str, Any]] = field(default_factory=list)


def _error(loc: list[Any], msg: str, kind: str) -> dict[str, Any]:
    return {"loc": loc, "msg": msg, "type": kind}


def validate_payload(raw: bytes, endpoint: str) -> Valid | Invalid:
    model = MODELS.get(endpoint)
    if model is None:
        return Invalid([_error([], "unknown endpoint", "unknown_endpoint")])
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):   # RecursionError: absurdly deep nesting
        return Invalid([_error([], "body is not valid JSON", "json_invalid")])
    try:
        payload = model.model_validate(obj, strict=True)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
        return Invalid([_error([str(part)[:MAX_LOC_PART] for part in e["loc"]], e["msg"], e["type"]) for e in errors])
    if payload.source != endpoint:
        return Invalid([_error(["source"], f"source must be '{endpoint}'", "source_mismatch")])
    return Valid(payload)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/domain/test_models.py -v`
Expected: PASS (all tests in the listed files).

- [ ] **Step 5: Commit**

```bash
git add src/patient360/domain/models.py src/patient360/domain/validate.py tests/domain/test_models.py
git commit -m "feat: strict payload validation"
```

---

### Task 8: Test packages: data types, workbook import step, ai.packages key

**Files:**
- Create: `src/patient360/domain/packages.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Package(package_id, name, classification, test_ids: frozenset[int])`, `PackageMatch(rank, package_id, name, classification, matched_test_ids, score=None)`, `attach_packages(output, matches, test_names) -> dict` (adds `ai.packages`), `forward_fill_packages(rows) -> (packages: dict[int, str], items: list[tuple[int, int, str]])`. The selection rule `(recommended_test_ids, packages) -> list[PackageMatch]` is added later when the owner provides it.

- [ ] **Step 1: Write the failing tests**

`tests/domain/test_packages.py`:

```python
import pytest

from patient360.domain.packages import PackageMatch, attach_packages, forward_fill_packages


def match(package_id=7, name="Kidney", tests=(1, 2), *, classification=None, score=None):
    return PackageMatch(1, package_id, name, classification, tests, score)


def test_attach_packages_adds_the_list_under_ai():
    out = attach_packages({"ai": {"summary": {"en": "x"}}}, [match()], {1: "Estimated (eGFR)", 2: "Uric acid"})
    assert out["ai"]["summary"] == {"en": "x"}
    assert out["ai"]["packages"] == [
        {"rank": 1, "package_id": 7, "name": "Kidney", "matched_tests": ["Estimated (eGFR)", "Uric acid"]}]


def test_attach_packages_keeps_classification_and_score_only_when_set():
    out = attach_packages({}, [match(classification="Routine", score=0.5)], {1: "A", 2: "B"})
    assert out["ai"]["packages"][0]["classification"] == "Routine" and out["ai"]["packages"][0]["score"] == 0.5


def test_no_matches_gives_an_empty_list_and_creates_ai_if_missing():
    assert attach_packages({}, [], {}) == {"ai": {"packages": []}}


def test_attach_packages_does_not_mutate_its_input():
    source = {"ai": {}}
    attach_packages(source, [match()], {})
    assert source == {"ai": {}}


def test_attach_packages_survives_a_test_without_a_known_name():
    out = attach_packages({}, [match()], {1: "A"})
    assert out["ai"]["packages"][0]["matched_tests"] == ["A", "2"]


def test_forward_fill_keeps_a_package_without_tests():
    rows = [
        (90, "Post-Bariatric  Surgery", 612, "Serum Iron"),
        (None, None, 87, "Total HCG (Quantitative)"),
        (6283, "Empty package", None, None),
        (91, "Tumor Marker screening", 5, "AFP"),
    ]
    packages, items = forward_fill_packages(rows)
    assert packages == {90: "Post-Bariatric Surgery", 6283: "Empty package", 91: "Tumor Marker screening"}
    assert items == [(90, 612, "Serum Iron"), (90, 87, "Total HCG (Quantitative)"), (91, 5, "AFP")]


def test_a_workbook_row_with_a_name_but_no_id_is_skipped_and_a_blank_name_is_empty_text():
    packages, items = forward_fill_packages([(1, "P", None, "orphan name"), (None, None, 7, None)])
    assert items == [(1, 7, "")]


def test_forward_fill_rejects_a_test_row_before_any_package():
    with pytest.raises(ValueError):
        forward_fill_packages([(None, None, 1, "x")])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/domain/test_packages.py -v`
Expected: FAIL with `ModuleNotFoundError` (or `ImportError`) for the module under test.

- [ ] **Step 3: Write the implementation**

`src/patient360/domain/packages.py`:

```python
"""Test packages: data types, the workbook import step, and the ``ai.packages`` output key.

The SELECTION RULE (which packages, how many, in what order) is not written yet: the owner will provide it.
When it exists it will be one function ``(recommended_test_ids, packages) -> list[PackageMatch]`` in this file.
"""
from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Package:
    package_id: int
    name: str
    classification: str | None
    test_ids: frozenset[int]


@dataclass(frozen=True)
class PackageMatch:
    rank: int
    package_id: int
    name: str
    classification: str | None
    matched_test_ids: tuple[int, ...]
    score: float | None = None      # only if the owner's rule produces one


def attach_packages(
    output: Mapping[str, Any], matches: Sequence[PackageMatch], test_names: Mapping[int, str]
) -> dict[str, Any]:
    """Add ``ai.packages`` to an output (internal or public). Keys with no value are omitted."""
    out = copy.deepcopy(dict(output))
    packages = []
    for m in matches:
        item = {
            "rank": m.rank, "package_id": m.package_id, "name": m.name,
            "classification": m.classification,
            "matched_tests": [test_names.get(t, str(t)) for t in m.matched_test_ids],
            "score": m.score,
        }
        packages.append({k: v for k, v in item.items() if v is not None})
    out.setdefault("ai", {})["packages"] = packages
    return out


def forward_fill_packages(
    rows: Iterable[tuple[Any, Any, Any, Any]],
) -> tuple[dict[int, str], list[tuple[int, int, str]]]:
    """Workbook rows ``(profile_id, profile_name, test_id, test_name)`` -> (packages, package-test items).

    The package columns are filled only on the first row of each package; later rows are blank.
    A package with no test rows stays in ``packages`` and has no item.
    """
    packages: dict[int, str] = {}
    items: list[tuple[int, int, str]] = []
    current: int | None = None
    for profile_id, profile_name, test_id, test_name in rows:
        if profile_id is not None:
            current = int(profile_id)
            packages[current] = " ".join(str(profile_name).split())
        if current is None:
            raise ValueError("test row before the first package row")
        if test_id is None:        # a name without an id cannot be keyed
            continue
        items.append((current, int(test_id), " ".join(str(test_name or "").split())))
    return packages, items
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/domain/test_packages.py -v`
Expected: PASS (all tests in the listed files).

- [ ] **Step 5: Commit**

```bash
git add src/patient360/domain/packages.py tests/domain/test_packages.py
git commit -m "feat: package types, workbook import step and ai.packages output key"
```

---

### Task 9: Normalised PostgreSQL schema

**Files:**
- Create: `migrations/001_init.sql`

**Interfaces:**
- Consumes: nothing.
- Produces: Tables `visits, batches, records, record_payloads, rejected_payloads, catalogue_tests, test_aliases, test_packages, package_tests, lab_results, ai_outputs, ai_recommendations, record_packages, record_package_tests, claims, claim_attempts`; views `record_payloads_current`, `claim_trace`.

- [ ] **Step 1: Write the failing tests**

`tests/test_schema.py`:

```python
"""Pins the schema decisions from the design review (no database needed). Run `psql -f` for the real check."""
import re
from pathlib import Path

SQL = re.sub(r"[ \t]+", " ", (Path(__file__).parent.parent / "migrations" / "001_init.sql").read_text(encoding="utf-8"))


def table(name: str) -> str:
    match = re.search(rf"CREATE TABLE {name} \((.*?)\n\)", SQL, re.S)
    assert match, f"CREATE TABLE {name} is missing"
    return match.group(1)


def test_every_expected_table_is_declared():
    for name in ["visits", "batches", "records", "record_payloads", "rejected_payloads", "catalogue_tests",
                 "test_aliases", "test_packages", "package_tests", "lab_results", "ai_outputs",
                 "ai_recommendations", "record_packages", "record_package_tests", "claims", "claim_attempts"]:
        table(name)


def test_real_foreign_keys_not_conceptual_ones():
    records = table("records")
    assert "REFERENCES visits (visit_id)" in records
    assert "REFERENCES batches (batch_id)" in records


def test_no_circular_reference_and_no_array_of_ids():
    assert "last_record_id" not in SQL                         # visits -> records back-reference removed
    assert "record_ids" not in SQL                             # an array cannot be foreign-keyed
    assert "text[]" not in SQL


def test_claims_has_one_current_row_per_record_and_a_pending_index():
    claims = table("claims")
    assert "record_id text PRIMARY KEY REFERENCES records (record_id)" in claims
    assert "CREATE INDEX claims_pending ON claims (updated_at) WHERE status = 'pending'" in SQL


def test_json_lives_in_the_payload_table_not_on_the_hot_rows():
    assert "jsonb" not in table("records") and "bytea" not in table("records")
    payloads = table("record_payloads")
    assert "jsonb_typeof(body) = 'object'" in payloads and "raw_bytes bytea" in payloads


def test_rejected_payloads_do_not_require_ids():
    rejected = table("rejected_payloads")
    assert "visit_id" not in rejected and "patient_id" not in rejected


def test_hot_tables_are_tuned_for_frequent_updates():
    for name in ("visits", "records", "claims"):
        assert re.search(rf"CREATE TABLE {name} \(.*?\) WITH \(fillfactor = 70", SQL, re.S)


def test_changed_data_is_a_new_record_so_there_is_no_second_dedupe_rule():
    assert "records_visit_dedupe" not in SQL and "repaired" not in SQL
    assert "record_id text PRIMARY KEY" in table("records")          # the key already contains source_hash


def test_repeated_parameters_and_the_package_cap_are_not_blocked_by_the_schema():
    assert "lab_result_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY" in table("lab_results")
    assert "BETWEEN 1 AND 7" not in SQL
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_schema.py -v`
Expected: FAIL with `ModuleNotFoundError` (or `ImportError`) for the module under test.

- [ ] **Step 3: Write the implementation**

`migrations/001_init.sql`:

```sql
-- Patient-360 pilot: initial schema (PostgreSQL 16+). One transaction, no circular foreign keys.
BEGIN;

-- ===== business progress per visit ================================================================
CREATE TABLE visits (
  visit_id    bigint PRIMARY KEY,
  patient_id  bigint NOT NULL,
  status      text NOT NULL DEFAULT 'received'
              CHECK (status IN ('received','processing','sent_to_aws','output_received','delivered','failed')),
  tier        text CHECK (tier IN ('NORMAL','MILD','MODERATE','CRITICAL')),
  lane        text CHECK (lane IN ('normal','critical')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
) WITH (fillfactor = 70, autovacuum_vacuum_scale_factor = 0.02);

CREATE TABLE batches (
  batch_id    text PRIMARY KEY,
  lane        text NOT NULL CHECK (lane IN ('normal','critical')),
  aws_job_id  text,
  status      text NOT NULL DEFAULT 'created'
              CHECK (status IN ('created','submitted','completed','partially_completed','failed')),
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- ===== one row per accepted payload: narrow header only (the JSON lives in record_payloads) ========
CREATE TABLE records (
  record_id    text PRIMARY KEY,                       -- HMAC(patient|visit|endpoint|source_hash); UUID for symptom checks
  visit_id     bigint NOT NULL REFERENCES visits (visit_id),
  endpoint     text NOT NULL CHECK (endpoint IN ('on-demand','urgent','batch','symptom-checker')),
  company_name text,
  age_years    int,
  sex          text CHECK (sex IN ('male','female')),
  lane         text CHECK (lane IN ('normal','critical')),
  tier         text CHECK (tier IN ('NORMAL','MILD','MODERATE','CRITICAL')),
  source_hash  text NOT NULL,                          -- part of record_id: changed data = new record
  batch_id     text REFERENCES batches (batch_id),
  stage        text NOT NULL DEFAULT 'received'
               CHECK (stage IN ('received','validated','processed','queued','sent_to_aws',
                                'output_received','delivered','failed')),
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
) WITH (fillfactor = 70, autovacuum_vacuum_scale_factor = 0.02);
CREATE INDEX records_visit_latest ON records (visit_id, created_at DESC);   -- "latest record of a visit"
CREATE INDEX records_batch ON records (batch_id) WHERE batch_id IS NOT NULL;

-- ===== the JSON itself, insert-only and versioned (normalised: one row per kind and version) ======
CREATE TABLE record_payloads (
  record_id  text NOT NULL REFERENCES records (record_id),
  kind       text NOT NULL CHECK (kind IN ('raw','processed','output_internal','output_public')),
  version    int  NOT NULL DEFAULT 1,
  body       jsonb NOT NULL CHECK (jsonb_typeof(body) = 'object'),   -- queryable, NOT byte-exact
  raw_bytes  bytea,                                                  -- exactly what the Backend sent
  sha256     text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (record_id, kind, version),
  CHECK ((kind = 'raw') = (raw_bytes IS NOT NULL))
);
CREATE VIEW record_payloads_current AS
  SELECT DISTINCT ON (record_id, kind) *
  FROM record_payloads ORDER BY record_id, kind, version DESC;

-- payloads that failed validation: they may lack ids, so they never go into records
CREATE TABLE rejected_payloads (
  rejected_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  endpoint    text NOT NULL,
  raw_bytes   bytea NOT NULL,
  errors      jsonb NOT NULL,
  blob_path   text,
  received_at timestamptz NOT NULL DEFAULT now()
);

-- ===== reference data: catalogue and test packages =================================================
CREATE TABLE catalogue_tests (
  test_id     int PRIMARY KEY,
  name        text NOT NULL,
  name_norm   text NOT NULL UNIQUE,                    -- lower-case, single spaces
  description text
);
CREATE TABLE test_aliases (
  alias_norm text PRIMARY KEY,
  test_id    int NOT NULL REFERENCES catalogue_tests (test_id)
);
CREATE TABLE test_packages (
  package_id     int PRIMARY KEY,                      -- the workbook "Profile ID"
  name           text NOT NULL UNIQUE,
  classification text,                                 -- empty in the workbook today; meaning to be confirmed
  active         boolean NOT NULL DEFAULT true
);
CREATE TABLE package_tests (
  package_id       int  NOT NULL REFERENCES test_packages (package_id),
  source_test_id   int  NOT NULL,
  source_test_name text NOT NULL,
  test_id          int  REFERENCES catalogue_tests (test_id),   -- NULL until aliased to the catalogue
  PRIMARY KEY (package_id, source_test_id)
);
CREATE INDEX package_tests_test ON package_tests (test_id) WHERE test_id IS NOT NULL;

-- ===== structured copies of the keys (analysis without parsing JSON) ================================
CREATE TABLE lab_results (
  lab_result_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,   -- a repeated parameter is allowed
  record_id          text NOT NULL REFERENCES records (record_id),
  test_name          text NOT NULL,
  parameter_name     text NOT NULL,
  result_date        date,
  value_text         text,
  value_num          numeric,
  unit               text,
  ref_low            numeric,
  ref_high           numeric,
  ref_range          text,
  status             text CHECK (status IN ('high','low','normal','abnormal')),
  lab_flag           text CHECK (lab_flag IN ('High','Low')),
  alert              text CHECK (alert IN ('NORMAL','MEDICAL_ATTENTION','CRITICAL','NOT_CLASSIFIED')),
  critical_low       numeric,
  critical_high      numeric,
  critical_direction text CHECK (critical_direction IN ('critical_low','critical_high')),
  previous_value     text,
  previous_date      date,
  parameter_remark   text
);
CREATE INDEX lab_results_record ON lab_results (record_id);

CREATE TABLE ai_outputs (
  record_id            text PRIMARY KEY REFERENCES records (record_id),
  status               text NOT NULL CHECK (status IN ('success','guardrail_blocked','error','answered')),
  highest_alert        text,
  summary_en           text, summary_ar text,
  simple_summary_en    text, simple_summary_ar text,
  note_en              text, note_ar text,
  critical_alerts      jsonb,
  advisory_level       text,
  guardrail_action     text CHECK (guardrail_action IN ('NONE','INTERVENED')),
  model_id             text,                           -- INTERNAL ONLY
  translation_model_id text,                           -- INTERNAL ONLY
  input_tokens         int, output_tokens int,         -- INTERNAL ONLY
  cost_usd             numeric(10,5),                  -- INTERNAL ONLY
  generated_at         timestamptz
);
CREATE TABLE ai_recommendations (
  record_id         text NOT NULL REFERENCES records (record_id),
  catalogue_test_id int  NOT NULL REFERENCES catalogue_tests (test_id),
  priority          text NOT NULL CHECK (priority IN ('urgent','important')),
  reason_en text, reason_ar text, timing_en text, timing_ar text, guideline_en text, guideline_ar text,
  PRIMARY KEY (record_id, catalogue_test_id)
);

-- test packages chosen for a record (rules in the application, results here)
CREATE TABLE record_packages (
  record_id  text NOT NULL REFERENCES records (record_id),
  package_id int  NOT NULL REFERENCES test_packages (package_id),
  rank       int  NOT NULL CHECK (rank >= 1),            -- the cap (if any) lives in the selection rule
  score      numeric(6,4),                              -- only if the owner's rule produces one
  PRIMARY KEY (record_id, package_id)
);
CREATE TABLE record_package_tests (
  record_id  text NOT NULL,
  package_id int  NOT NULL,
  test_id    int  NOT NULL REFERENCES catalogue_tests (test_id),
  PRIMARY KEY (record_id, package_id, test_id),
  FOREIGN KEY (record_id, package_id) REFERENCES record_packages (record_id, package_id)
);

-- ===== execution state: who is working on which record, and retries ================================
CREATE TABLE claims (                                   -- exactly ONE current claim per record
  record_id       text PRIMARY KEY REFERENCES records (record_id),
  read_from       text NOT NULL CHECK (read_from IN ('blob','postgresql')),
  status          text NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','processing','sent','delivered','failed')),
  claimed_by      text,                                 -- e.g. 'normal-batch-endpoint', 'urgent'
  idempotency_key text NOT NULL,
  attempts        int  NOT NULL DEFAULT 0,
  next_retry_at   timestamptz,
  last_error      text,
  updated_at      timestamptz NOT NULL DEFAULT now()
) WITH (fillfactor = 70, autovacuum_vacuum_scale_factor = 0.02);
CREATE INDEX claims_pending ON claims (updated_at) WHERE status = 'pending';

CREATE TABLE claim_attempts (                           -- insert-only audit trail of every attempt
  claim_attempt_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  record_id        text NOT NULL REFERENCES records (record_id),
  attempt          int  NOT NULL,
  started_at       timestamptz NOT NULL DEFAULT now(),
  finished_at      timestamptz,
  outcome          text CHECK (outcome IN ('sent','retry','failed')),
  error            text
);
CREATE INDEX claim_attempts_record ON claim_attempts (record_id);

-- the tracing view the owner described: visit | source | status | claimed-by
CREATE VIEW claim_trace AS
  SELECT r.visit_id, c.read_from AS source, c.status, c.claimed_by
  FROM claims c JOIN records r USING (record_id);

COMMIT;
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_schema.py -v`
Expected: PASS (all tests in the listed files).

- [ ] **Step 4b: Run the migration against a real PostgreSQL (the text tests above do not parse SQL)**

Run: `psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/001_init.sql` on an empty database.
Expected: ends with `COMMIT` and no error. Then `psql "$env:DATABASE_URL" -c "\dt"` lists the 16 tables. If it fails, fix the SQL and keep `tests/test_schema.py` green.

- [ ] **Step 5: Commit**

```bash
git add migrations/001_init.sql tests/test_schema.py
git commit -m "feat: normalised postgres schema"
```

---

## Full run

- [ ] Run `uv run pytest -q`. Expected: `112 passed` (also passed on Python 3.14 and 3.15.0b4).
- [ ] Run `uv run ruff check .`. Expected: `All checks passed!`
- [ ] Run `uv run lint-imports`. Expected: `domain stays pure: no I/O libraries KEPT`, `Contracts: 1 kept, 0 broken.`

## Self-Review

- **Spec coverage:** validation (T7), keys added/removed and classification (T2-T3), tier and lane (T4), public output (T5), ids (T6), package types and output key (T8), normalised schema with `record_payloads`, `rejected_payloads`, FKs, claims and trace view (T9). Not in this plan, by design: Blob/Postgres/Bedrock adapters, dispatcher and batch trigger, HTTP endpoints, Backend delivery with `Idempotency-Key`, the workbook importer (only its pure `forward_fill_packages` step), structlog setup, Dockerfile, GitHub Actions workflow and migration runner (they need the app entry point from the API plan). Each gets its own later plan; README section 12.4 lists the libraries and connections they will use.
- **Placeholders:** none; every code block is the file that passed the suite (112 tests, Python 3.13.14, Pydantic v2).
- **Type consistency:** `LogicResult.flat` (T3) feeds `classify_patient` (T4); `Package.test_ids` and `PackageMatch.matched_test_ids` are used identically in T7 code and tests.
- **Not run:** `migrations/001_init.sql` was never executed against PostgreSQL (no `psql` on the build machine); Task 9 Step 4b is the real check. Python 3.15 and 3.14 were not tested locally.
- **Package selection and scoring:** deliberately left out; the owner will provide the rules. `record_packages.score` is nullable for that reason, and `classification` is empty in the workbook today.
