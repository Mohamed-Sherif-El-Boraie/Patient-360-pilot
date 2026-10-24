"""Deterministic stand-in for the RAG step (pilot): catalogue tests in the category of a not-normal parameter."""
from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from patient360.domain.rules.classify import has_value

MAX_RETRIEVED_TESTS = 50


@dataclass(frozen=True)
class CatalogueTest:
    test_id: int
    test_name_en: str
    test_description_en: str | None
    category_ids: tuple[int, ...] = ()   # ``tests.category_id``
    active: bool = True


@dataclass(frozen=True)
class CatalogueParameter:
    parameter_id: int
    category_ids: tuple[int, ...] = ()   # ``parameters.category_id``
    active: bool = True


def build_catalogue_maps(
    active_category_ids: Collection[int], tests: Iterable[CatalogueTest], parameters: Iterable[CatalogueParameter],
) -> tuple[dict[int, tuple[int, ...]], dict[int, list[CatalogueTest]]]:
    """The two maps ``select_retrieved_tests`` needs, from the rows of the database. ``active = false`` means kept for
    history, never used to pick tests:

    - an inactive test is never recommended;
    - an inactive category is ignored, for its parameters and for its tests (a test also listed in an active category
      is still found through that one); an id that is not an active category (a typo) is ignored too;
    - an inactive parameter is still judged and counted in the tier, but picks no tests.

    Returns ``(parameter_id -> category ids, category_id -> tests)``.
    """
    active = set(active_category_ids)
    by_parameter = {p.parameter_id: tuple(c for c in p.category_ids if c in active) for p in parameters if p.active}
    by_category: dict[int, list[CatalogueTest]] = {}
    for test in tests:
        if test.active:
            for category_id in dict.fromkeys(test.category_ids):
                if category_id in active:
                    by_category.setdefault(category_id, []).append(test)
    return by_parameter, by_category


def _same(name: str) -> str:
    return " ".join(str(name).split()).casefold()


def select_retrieved_tests(
    rows: Sequence[Mapping[str, Any]],
    parameter_categories: Mapping[int, Collection[int]],   # parameter_id -> its category ids
    tests_by_category: Mapping[int, Sequence[CatalogueTest]],     # category_id -> the tests in it
    performed_test_names: Iterable[str],
    max_tests: int = MAX_RETRIEVED_TESTS,
    include_normal: bool = False,
) -> list[dict[str, Any]]:
    """All parameters normal -> nothing. Otherwise every catalogue test in the categories of the not-normal parameters,
    minus the tests the patient already did, at most ``max_tests``. Categories of CRITICAL parameters come first, then
    the other not-normal ones; inside a category the order is by test id.
    ``include_normal`` (a symptom check of a NORMAL patient: the symptoms may call for a test) also takes the
    categories of the normal parameters, after the others.
    Output: ``retrieved_tests`` for the AI input JSON.
    """
    valued = [row for row in rows if has_value(row)]
    not_normal = [row for row in valued if row["status"] != "normal"]
    def categories_of(selected: Iterable[Mapping[str, Any]]) -> set[int]:
        """A parameter can be in several categories."""
        return {category for r in selected for category in parameter_categories.get(r["parameter_id"], ())}

    critical = categories_of(r for r in not_normal if r["status"] == "critical")
    others = categories_of(not_normal) - critical
    normal = categories_of(valued) - critical - others
    ordered = sorted(critical) + sorted(others) + (sorted(normal) if include_normal else [])
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
