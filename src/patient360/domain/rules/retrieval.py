"""Deterministic stand-in for the RAG step (pilot): catalogue tests in the category of a not-normal parameter."""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from patient360.domain.rules.classify import has_value

MAX_RETRIEVED_TESTS = 50


@dataclass(frozen=True)
class CatalogueTest:
    test_id: int
    test_name_en: str
    test_description_en: str | None
    category: str | None


def _same(name: str) -> str:
    return " ".join(str(name).split()).casefold()


def select_retrieved_tests(
    rows: Sequence[Mapping[str, Any]],
    parameter_categories: Mapping[int, str | None],
    tests_by_category: Mapping[str, Sequence[CatalogueTest]],
    performed_test_names: Iterable[str],
    max_tests: int = MAX_RETRIEVED_TESTS,
) -> list[dict[str, Any]]:
    """All parameters normal -> nothing. Otherwise every catalogue test in the categories of the not-normal parameters,
    minus the tests the patient already did, at most ``max_tests``. Categories of CRITICAL parameters come first, then
    the other not-normal ones; inside a category the order is by test id.
    Output: ``retrieved_tests`` for the AI input JSON.
    """
    not_normal = [row for row in rows if has_value(row) and row["status"] != "normal"]
    critical = {parameter_categories.get(r["parameter_id"]) for r in not_normal if r["status"] == "critical"} - {None}
    others = {parameter_categories.get(r["parameter_id"]) for r in not_normal} - {None} - critical
    ordered = sorted(critical) + sorted(others)
    performed = {_same(name) for name in performed_test_names}
    seen: set[int] = set()
    retrieved: list[dict[str, Any]] = []
    for category in ordered:
        for test in sorted(tests_by_category.get(category, ()), key=lambda t: t.test_id):
            if test.test_id in seen or _same(test.test_name_en) in performed:
                continue
            seen.add(test.test_id)
            retrieved.append({"test_name": test.test_name_en, "test_description": test.test_description_en})
            if len(retrieved) == max_tests:
                return retrieved
    return retrieved
