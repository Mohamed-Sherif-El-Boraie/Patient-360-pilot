"""Loads the catalogue (categories, tests, parameters, packages and the tests inside each package) from the Excel
workbook into PostgreSQL: ``python -m patient360.load_catalogue [workbook.xlsx] [--dry-run]``. The image runs it at
start-up (after the migrations); run it again whenever the workbook changes.

The workbook is the argument, else the environment variable CATALOGUE_WORKBOOK, else ``catalogue/`` next to
``migrations/``. It reads the same PG* variables as ``patient360.migrate``. It adds new rows and updates the rows that
exist. It never deletes. The ``active`` column of a sheet is applied when the sheet has one; without it, new rows are
active and existing rows keep their ``active``. An advisory lock makes two instances starting together load one after
the other. ``--dry-run`` checks the workbook and prints the counts, without a database.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import openpyxl
import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import tuple_row

from patient360.migrate import schema_from_env, use_schema

LOCK_ID = 360_001_002                         # migrate.py uses 360_001_001
DEFAULT_WORKBOOK = Path(__file__).resolve().parents[2] / "catalogue" / "Packages_tests_paramters.xlsx"
REQUIRED = ("PGHOST", "PGDATABASE", "PGUSER", "PGPASSWORD")
PACKAGE_TYPES = ("WELLNESS", "GENERAL")
NULL_WORDS = {"", "null", "none", "nan"}      # the workbook writes the text NULL for an empty cell


@dataclass
class Records:
    categories: dict[int, str] = field(default_factory=dict)
    category_active: dict[int, bool | None] = field(default_factory=dict)
    tests: dict[int, dict[str, Any]] = field(default_factory=dict)
    parameters: dict[int, dict[str, Any]] = field(default_factory=dict)
    packages: dict[int, dict[str, Any]] = field(default_factory=dict)
    links: set[tuple[int, int]] = field(default_factory=set)       # (package_id, test_id)
    warnings: list[str] = field(default_factory=list)


def read_sheets(path: Path) -> dict[str, list[tuple]]:
    """Every sheet as its rows (the header row first)."""
    book = openpyxl.load_workbook(path, data_only=True, read_only=True)
    return {sheet.title: list(sheet.iter_rows(values_only=True)) for sheet in book.worksheets}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return None if text.lower() in NULL_WORDS else text


def _whole(value: Any) -> int | None:
    text = _text(value)
    try:
        return None if text is None else int(float(text))
    except ValueError:
        return None


def _flag(value: Any) -> bool | None:
    """The ``active`` cell: TRUE / FALSE (or 1 / 0, yes / no); an empty cell is None (= leave as it is)."""
    if isinstance(value, bool):
        return value
    text = (_text(value) or "").lower()
    return True if text in ("true", "1", "yes") else False if text in ("false", "0", "no") else None


def _index(header: list[str], sheet: str, name: str) -> int:
    if name not in header:
        raise SystemExit(f"Sheet '{sheet}' has no column '{name}' (it has: {', '.join(header)}).")
    return header.index(name)


def _active(row: tuple, header: list[str]) -> bool | None:
    return _flag(row[header.index("active")]) if "active" in header else None


def _categories(value: Any) -> list[int]:
    """``"4; 14; 37"`` (or the number 15) -> [4, 14, 37]."""
    text = _text(value)
    return [] if text is None else [int(part) for part in re.split(r"[;,]", text) if part.strip().isdigit()]


def _rows(sheets: dict[str, list[tuple]], name: str, header_start: str) -> list[tuple]:
    rows = sheets.get(name)
    if not rows or not str(rows[0][0]).strip().lower().startswith(header_start):
        raise SystemExit(f"The workbook has no sheet '{name}' starting with the column '{header_start}'.")
    return [row for row in rows[1:] if any(cell is not None for cell in row)]


def build_records(sheets: dict[str, list[tuple]]) -> Records:
    """The sheets as clean rows. A bad row is skipped and listed in ``warnings``; it never stops the load."""
    out = Records()
    category_header = [str(cell).strip() for cell in sheets["Categories"][0]] if "Categories" in sheets else []
    category_active: dict[int, bool | None] = {}
    for row in _rows(sheets, "Categories", "category_id"):
        if (category_id := _whole(row[0])) is not None and (name := _text(row[1])):
            out.categories[category_id] = name
            category_active[category_id] = _active(row, category_header)
    out.category_active = category_active

    header = [str(cell).strip() for cell in sheets["Tests"][0]]
    column = {name: _index(header, "Tests", name) for name in (
        "test_name_en", "test_name_ar", "test_description_en", "test_description_ar", "test_precautions_en",
        "test_precautions_ar", "category_id")}
    for row in _rows(sheets, "Tests", "test_id"):
        test_id, name = _whole(row[0]), _text(row[column["test_name_en"]])
        if test_id is None or name is None or test_id in out.tests:
            out.warnings.append(f"Tests: skipped row with test_id={row[0]!r} (missing id or name, or a repeated id)")
            continue
        out.tests[test_id] = {
            "test_id": test_id, "test_name_en": name, "test_name_ar": _text(row[column["test_name_ar"]]),
            "test_description_en": _text(row[column["test_description_en"]]),
            "test_description_ar": _text(row[column["test_description_ar"]]),
            "test_precautions_en": _text(row[column["test_precautions_en"]]),
            "test_precautions_ar": _text(row[column["test_precautions_ar"]]),
            "category_id": _categories(row[column["category_id"]]), "active": _active(row, header)}

    parameter_header = [str(cell).strip() for cell in sheets["Parameters"][0]] if "Parameters" in sheets else []
    for row in _rows(sheets, "Parameters", "param"):               # the sheet spells it "paramter_id"
        parameter_id, name = _whole(row[0]), _text(row[1])
        if parameter_id is None or name is None or parameter_id in out.parameters:
            out.warnings.append(f"Parameters: skipped row with id={row[0]!r} (missing id or name, or a repeated id)")
            continue
        out.parameters[parameter_id] = {"parameter_id": parameter_id, "parameter_name": name,
                                        "category_id": _categories(row[2]), "active": _active(row, parameter_header)}

    package_header = [str(cell).strip() for cell in sheets["Packages"][0]] if "Packages" in sheets else []
    pc = {name: _index(package_header, "Packages", name) for name in (
        "package_id", "package_name_en", "package_name_ar", "package_type", "package_description_en",
        "package_description_ar", "sex_id", "age_min", "age_max")} if package_header else {}
    for row in _rows(sheets, "Packages", "package_id"):
        package_id, name, kind = _whole(row[pc["package_id"]]), _text(row[pc["package_name_en"]]), (
            _text(row[pc["package_type"]]) or "").upper()
        sex_id, age_min, age_max = _whole(row[pc["sex_id"]]), _whole(row[pc["age_min"]]), _whole(row[pc["age_max"]])
        bad = (package_id is None or name is None or kind not in PACKAGE_TYPES or sex_id not in (1, 2, 3)
               or (age_min is not None and age_min < 0) or (age_min is not None and age_max is not None
                                                            and age_min > age_max) or package_id in out.packages)
        if bad:
            out.warnings.append(f"Packages: skipped row with package_id={row[0]!r}")
            continue
        out.packages[package_id] = {
            "package_id": package_id, "package_name_en": name, "package_name_ar": _text(row[pc["package_name_ar"]]),
            "package_type": kind, "package_description_en": _text(row[pc["package_description_en"]]),
            "package_description_ar": _text(row[pc["package_description_ar"]]), "sex_id": sex_id,
            "age_min": age_min, "age_max": age_max, "active": _active(row, package_header)}

    missing = 0
    for row in _rows(sheets, "Packages Linked Tests", "test_id"):    # test first, then package
        test_id, package_id = _whole(row[0]), _whole(row[1])
        if test_id in out.tests and package_id in out.packages:
            out.links.add((package_id, test_id))
        else:
            missing += 1
    if missing:
        out.warnings.append(f"Packages Linked Tests: skipped {missing} link(s) to a test or package that is not listed")
    unknown = {c for item in (*out.tests.values(), *out.parameters.values()) for c in item["category_id"]
               if c not in out.categories}
    if unknown:
        out.warnings.append(f"category ids used by tests or parameters but missing from Categories: {sorted(unknown)}")
    return out


def load(conn, records: Records) -> dict[str, int]:
    """Upsert everything in the caller's transaction; returns the row count of each table afterwards."""
    with conn.cursor(row_factory=tuple_row) as cursor:      # whatever row type the connection uses, here it is a tuple
        cursor.executemany(
            "INSERT INTO categories (category_id, category_name, active) VALUES (%s, %s, COALESCE(%s, true)) "
            "ON CONFLICT (category_id) DO UPDATE SET category_name = EXCLUDED.category_name, "
            "active = COALESCE(%s, categories.active)",
            [(i, name, records.category_active.get(i), records.category_active.get(i))
             for i, name in records.categories.items()])
        cursor.executemany(
            "INSERT INTO tests (test_id, test_name_en, test_name_ar, test_description_en, test_description_ar, "
            "test_precautions_en, test_precautions_ar, category_id, active) VALUES (%(test_id)s, %(test_name_en)s, "
            "%(test_name_ar)s, %(test_description_en)s, %(test_description_ar)s, %(test_precautions_en)s, "
            "%(test_precautions_ar)s, %(category_id)s, COALESCE(%(active)s, true)) ON CONFLICT (test_id) DO UPDATE SET "
            "test_name_en = EXCLUDED.test_name_en, test_name_ar = EXCLUDED.test_name_ar, "
            "test_description_en = EXCLUDED.test_description_en, test_description_ar = EXCLUDED.test_description_ar, "
            "test_precautions_en = EXCLUDED.test_precautions_en, test_precautions_ar = EXCLUDED.test_precautions_ar, "
            "category_id = EXCLUDED.category_id, active = COALESCE(%(active)s, tests.active)",
            list(records.tests.values()))
        cursor.executemany(
            "INSERT INTO parameters (parameter_id, parameter_name, category_id, active) VALUES (%(parameter_id)s, "
            "%(parameter_name)s, %(category_id)s, COALESCE(%(active)s, true)) ON CONFLICT (parameter_id) DO UPDATE SET "
            "parameter_name = EXCLUDED.parameter_name, category_id = EXCLUDED.category_id, "
            "active = COALESCE(%(active)s, parameters.active)",
            list(records.parameters.values()))
        cursor.executemany(
            "INSERT INTO packages (package_id, package_name_en, package_name_ar, package_type, package_description_en, "
            "package_description_ar, sex_id, age_min, age_max, active) VALUES (%(package_id)s, %(package_name_en)s, "
            "%(package_name_ar)s, %(package_type)s, %(package_description_en)s, %(package_description_ar)s, "
            "%(sex_id)s, %(age_min)s, %(age_max)s, COALESCE(%(active)s, true)) ON CONFLICT (package_id) DO UPDATE SET "
            "package_name_en = EXCLUDED.package_name_en, package_name_ar = EXCLUDED.package_name_ar, "
            "package_type = EXCLUDED.package_type, package_description_en = EXCLUDED.package_description_en, "
            "package_description_ar = EXCLUDED.package_description_ar, sex_id = EXCLUDED.sex_id, "
            "age_min = EXCLUDED.age_min, age_max = EXCLUDED.age_max, active = COALESCE(%(active)s, packages.active)",
            list(records.packages.values()))
        cursor.executemany("INSERT INTO test_package_map (package_id, test_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                           sorted(records.links))
        counts = {}
        for table in ("categories", "tests", "parameters", "packages", "test_package_map"):
            counts[table] = cursor.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    return counts


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    paths = [a for a in argv if not a.startswith("--")]
    workbook = Path(paths[0] if paths else os.environ.get("CATALOGUE_WORKBOOK") or DEFAULT_WORKBOOK)
    if not dry_run and (missing := [name for name in REQUIRED if not os.environ.get(name)]):
        print(f"missing environment variables: {', '.join(missing)}", file=sys.stderr)
        return 2
    try:
        records = build_records(read_sheets(workbook))
        for warning in records.warnings:
            print("warning:", warning)
        print(f"workbook {workbook.name}: {len(records.categories)} categories, {len(records.tests)} tests, "
              f"{len(records.parameters)} parameters, {len(records.packages)} packages, "
              f"{len(records.links)} package links")
        if dry_run:
            print("dry run: nothing was written")
            return 0
        dsn = make_conninfo(host=os.environ["PGHOST"], port=os.environ.get("PGPORT", "5432"),
                            dbname=os.environ["PGDATABASE"], user=os.environ["PGUSER"],
                            password=os.environ["PGPASSWORD"], sslmode=os.environ.get("PGSSLMODE", "prefer"))
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            use_schema(conn, schema_from_env())
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))     # released at the commit below
            counts = load(conn, records)
    except SystemExit as error:                 # a workbook without the expected sheet or column
        print(f"catalogue load failed: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, psycopg.Error) as error:
        print(f"catalogue load failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("database now holds:", ", ".join(f"{count} {table}" for table, count in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
