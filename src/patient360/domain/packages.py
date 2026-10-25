"""Test packages: the data type and the ``ai.packages`` entries. The SELECTION RULE is not written yet (owner)."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Package:
    package_id: int
    name: str                    # package_name_en
    category: str | None         # ROUTINE / NON-ROUTINE (more may come)
    sex_id: int                  # 1 Male, 2 Female, 3 Any
    age_min: int | None          # years, None = no lower limit
    age_max: int | None          # years, None = no upper limit
    test_ids: frozenset[int]


def package_entries(packages: Sequence[Package], test_names: Mapping[int, str]) -> list[dict]:
    """The chosen packages (best first) as ``ai.packages``: a name and the tests inside each package."""
    return [
        {"package_name": p.name, "tests": [{"test_name": test_names.get(t, str(t))} for t in sorted(p.test_ids)]}
        for p in packages
    ]
