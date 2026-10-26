"""tools/load_catalogue.py: the workbook rows become clean records, and loading them is safe to repeat."""
import importlib.util
import sys
from pathlib import Path

import pytest

from tests.adapters.postgres.seed import fetch

_spec = importlib.util.spec_from_file_location(
    "load_catalogue", Path(__file__).parents[3] / "tools" / "load_catalogue.py")
loader = importlib.util.module_from_spec(_spec)
sys.modules["load_catalogue"] = loader          # a dataclass looks its own module up here
_spec.loader.exec_module(loader)

PACKAGE_HEADER = ("package_id", "package_name_en", "package_name_ar", "package_type", "package_description_en",
                  "package_description_ar", "sex_id", "age_min", "age_max", "active")
TEST_HEADER = ("test_id", "test_name_en", "test_name_ar", "test_description_ar", "test_description_en",
               "test_precautions_ar", "test_precautions_en", "category_id", "active")


def sheets():
    return {
        "Categories": [("category_id", "category_name", "active"), (4, "haematology", True), (15, "diabetes", True),
                       (30, "kidney", False)],
        "Tests": [TEST_HEADER,
                  (18, " Fasting Blood Glucose ", "الجلوكوز", "وصف", "Measures glucose", "NULL", "NULL", 15, True),
                  (8419, "STR chimerism", None, None, None, None, None, "4; 14; 37", False),
                  (None, "no id", None, None, None, None, None, 4, True),
                  (18, "repeated id", None, None, None, None, None, 4, True)],
        "Parameters": [("paramter_id", "paramter_name", "category_id", "active"),
                       (2, "Fasting Blood Glucose", 15, True),
                       (3, "Blood Urea", "30", "FALSE"), (3, "repeated", 30, True)],
        "Packages": [PACKAGE_HEADER,
                     (10596, "Essential Health Checkup", "الفحص", "WELLNESS", "A checkup", "فحص", 3, 0, 150, True),
                     (10597, "Men only", None, "general", None, None, 1, 18, 50, None),
                     (10598, "bad type", None, "OTHER", None, None, 3, 0, 5, True),
                     (10599, "bad ages", None, "GENERAL", None, None, 3, 30, 5, True)],
        "Packages Linked Tests": [("test_id", "package_id"), (18, 10596), (18, 10597), (999, 10596), (18, 77)],
    }


def test_the_workbook_rows_become_clean_records():
    records = loader.build_records(sheets())
    assert records.categories == {4: "haematology", 15: "diabetes", 30: "kidney"}
    glucose = records.tests[18]
    assert (glucose["test_name_en"], glucose["test_precautions_en"], glucose["category_id"]) == (
        "Fasting Blood Glucose", None, [15])                       # trimmed, and the text NULL is a real null
    assert records.tests[8419]["category_id"] == [4, 14, 37] and set(records.tests) == {18, 8419}
    assert records.parameters[3]["category_id"] == [30] and len(records.parameters) == 2
    essential = records.packages[10596]
    assert (essential["package_description_ar"], essential["sex_id"], essential["age_max"]) == ("فحص", 3, 150)
    assert records.packages[10597]["package_type"] == "GENERAL" and set(records.packages) == {10596, 10597}
    assert records.links == {(10596, 18), (10597, 18)}
    text = " | ".join(records.warnings)
    assert "repeated id" in text and "skipped 2 link(s)" in text and "[14, 37]" in text
    assert "package_id=10598" in text and "package_id=10599" in text


def test_the_active_column_of_the_sheet_is_read_and_an_empty_cell_means_leave_as_is():
    records = loader.build_records(sheets())
    assert (records.tests[18]["active"], records.tests[8419]["active"], records.parameters[3]["active"]) == (
        True, False, False)
    assert records.category_active == {4: True, 15: True, 30: False} and records.packages[10597]["active"] is None


def test_loading_fills_the_tables_and_can_be_repeated(db):
    records = loader.build_records(sheets())
    with db.transaction() as conn:
        first = loader.load(conn, records)
    assert first == {"categories": 3, "tests": 2, "parameters": 2, "packages": 2, "test_package_map": 2}
    assert fetch(db, "SELECT test_id, active FROM tests ORDER BY test_id") == [
        {"test_id": 18, "active": True}, {"test_id": 8419, "active": False}]       # the sheet decides
    assert fetch(db, "SELECT active FROM categories WHERE category_id = 30") == [{"active": False}]
    assert fetch(db, "SELECT active FROM packages WHERE package_id = 10597") == [{"active": True}]   # empty: default

    fetch(db, "UPDATE tests SET active = false WHERE test_id = 18 RETURNING 1")
    fetch(db, "UPDATE packages SET active = false WHERE package_id = 10597 RETURNING 1")
    records.tests[18]["test_name_en"] = "Fasting glucose (new name)"
    with db.transaction() as conn:
        assert loader.load(conn, records) == first                                      # the same counts again
    assert fetch(db, "SELECT test_name_en, active, category_id FROM tests WHERE test_id = 18") == [
        {"test_name_en": "Fasting glucose (new name)", "active": True, "category_id": [15]}]   # sheet says TRUE
    assert fetch(db, "SELECT active FROM packages WHERE package_id = 10597") == [{"active": False}]   # empty cell: kept
    assert fetch(db, "SELECT sex_id, age_min FROM packages WHERE package_id = 10597") == [{"sex_id": 1, "age_min": 18}]


def test_a_workbook_without_the_expected_sheet_stops_with_a_clear_message():
    broken = sheets()
    del broken["Packages"]
    with pytest.raises(SystemExit, match="Packages"):
        loader.build_records(broken)
