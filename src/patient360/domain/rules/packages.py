"""Test packages: the data type, the selection rule and the ``ai.packages`` entries. Pure.

The rule (owner's): from the packages that fit the patient's sex and age, take the ones that cover the tests the AI
recommended (best coverage first, no package that repeats tests already covered), then add the routine (wellness)
packages when nothing matched or from age 50, and never return more than ``MAX_PACKAGES``.
"""
from __future__ import annotations

import math
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

DAYS_PER_YEAR = 365.25          # age_days -> completed years, the unit of age_min and age_max
COVERAGE_WEIGHT = 0.8           # score = 0.8 * coverage + 0.2 * focus
CLOSE_SCORE_MARGIN = 0.05       # a package within this distance of the best score is a candidate too
ROUTINE_TYPES = frozenset({"WELLNESS"})
ROUTINE_MIN_AGE = 50            # from this age (completed years) the routine packages are always added
MAX_PACKAGES = 5                # the most packages returned in total


def normalize_name(name: object) -> str:
    """Lower case, trimmed, spaces collapsed: ``"HbA1c "`` equals ``"hba1c"``. Test names are matched by this."""
    return re.sub(r"\s+", " ", str(name).strip().lower())


@dataclass(frozen=True)
class Package:
    package_id: int
    name: str                    # package_name_en
    sex_id: int                  # 1 Male, 2 Female, 3 Both
    age_min: int | None          # years, None = no lower limit
    age_max: int | None          # years, None = no upper limit
    test_ids: frozenset[int]
    name_ar: str | None = None
    package_type: str = "GENERAL"            # WELLNESS or GENERAL


def _fits(package: Package, sex_id: int, age_years: int) -> bool:
    upper = math.inf if package.age_max is None else package.age_max
    return package.sex_id in (3, sex_id) and (package.age_min or 0) <= age_years <= upper


def _best_matches(suitable: Sequence[Package], requested: Collection[int]) -> list[Package]:
    """The packages that cover the requested tests: best score first, near-best only, none repeating covered tests."""
    scored = []
    for package in suitable:
        matched = package.test_ids & requested
        if matched:
            coverage = len(matched) / len(requested)          # how much of the need it covers
            focus = len(matched) / len(package.test_ids)      # how little unrelated it holds
            score = round(COVERAGE_WEIGHT * coverage + (1 - COVERAGE_WEIGHT) * focus, 4)
            scored.append((score, coverage, package))
    # ties: more coverage, then the smaller package, then the lower id: the same input always gives the same answer
    scored.sort(key=lambda s: (-s[0], -s[1], len(s[2].test_ids), s[2].package_id))
    chosen: list[Package] = []
    covered: set[int] = set()
    for score, _, package in scored:
        if scored[0][0] - score > CLOSE_SCORE_MARGIN:
            break
        matched = package.test_ids & requested
        if matched & covered:
            continue
        chosen.append(package)
        covered |= matched
        if len(chosen) == MAX_PACKAGES:
            break
    return chosen


def select_packages(packages: Sequence[Package], requested_test_ids: Collection[int], age_days: float,
                    sex_id: int) -> list[Package]:
    """The packages to show, best first. ``requested_test_ids``: the tests the AI recommended (known tests only)."""
    age_years = int(age_days // DAYS_PER_YEAR)
    suitable = [p for p in packages if _fits(p, sex_id, age_years)]
    chosen = _best_matches(suitable, set(requested_test_ids)) if requested_test_ids else []
    if not chosen or age_years >= ROUTINE_MIN_AGE:
        taken = {p.package_id for p in chosen}
        chosen += sorted((p for p in suitable if p.package_type in ROUTINE_TYPES and p.package_id not in taken),
                         key=lambda p: p.package_id)
    return chosen[:MAX_PACKAGES]


def package_entries(packages: Sequence[Package], test_names: Mapping[int, tuple[str, str | None]]) -> list[dict]:
    """The chosen packages (best first) as ``ai.packages``: English and Arabic names of the package and of every test in
    it. ``test_names``: test id -> (English, Arabic). A missing Arabic name shows the English one."""

    def pair(english: str, arabic: str | None) -> dict[str, str]:
        return {"en": english, "ar": arabic or english}

    return [
        {"package_name": pair(p.name, p.name_ar),
         "tests": [{"test_name": pair(*test_names.get(t, (str(t), None)))} for t in sorted(p.test_ids)]}
        for p in packages
    ]
