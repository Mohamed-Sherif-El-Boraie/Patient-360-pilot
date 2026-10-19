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
    name: str                    # the workbook's "Profile name"
    category: str | None         # ROUTINE / NON-ROUTINE (more may come)
    gender: str | None           # "male", "female" or None = any gender
    age_min: int | None          # years, None = no lower limit
    age_max: int | None          # years, None = no upper limit
    test_ids: frozenset[int]


@dataclass(frozen=True)
class PackageMatch:
    rank: int
    package_id: int
    name: str
    category: str | None
    matched_test_ids: tuple[int, ...]
    score: float | None = None      # only if the owner's rule produces one


def add_packages_to_output(
    output: Mapping[str, Any], matches: Sequence[PackageMatch], test_names: Mapping[int, str]
) -> dict[str, Any]:
    """Add ``ai.packages`` to an output (internal or public). Keys with no value are omitted."""
    out = copy.deepcopy(dict(output))
    packages = []
    for m in matches:
        item = {
            "rank": m.rank, "package_id": m.package_id, "name": m.name,
            "category": m.category,
            "matched_tests": [test_names.get(t, str(t)) for t in m.matched_test_ids],
            "score": m.score,
        }
        packages.append({k: v for k, v in item.items() if v is not None})
    out.setdefault("ai", {})["packages"] = packages
    return out


def fill_down_package_rows(
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
