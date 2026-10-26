"""PostgreSQL implementation of the ``Catalogue`` port: the reference data behind the retrieval rule."""
from __future__ import annotations

import time
from collections.abc import Callable, Collection, Mapping, Sequence
from typing import Any

from patient360.adapters.postgres.db import Database
from patient360.domain.rules.packages import Package, normalize_name
from patient360.domain.rules.retrieval import CatalogueParameter, CatalogueTest, build_catalogue_maps

PACKAGE_DATA_CACHE_S = 300      # the package data changes only when the catalogue is loaded: read it every 5 minutes


class PostgresCatalogue:
    def __init__(self, db: Database, *, cache_s: float = PACKAGE_DATA_CACHE_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        """``cache_s``: how long the package data is kept in memory. A batch result calls it once per visit, so the
        database is not asked 50,000 times. A catalogue load shows up within this time (or after a restart)."""
        self._db, self._cache_s, self._clock = db, cache_s, clock
        self._cached: dict[str, tuple[float, Any]] = {}

    def _cache(self, name: str, read: Callable[[], Any]) -> Any:
        now = self._clock()
        found = self._cached.get(name)
        if found is None or now - found[0] >= self._cache_s:
            found = self._cached[name] = (now, read())
        return found[1]

    def _load_maps(self) -> tuple[dict[int, tuple[int, ...]], dict[int, list[CatalogueTest]]]:
        """Read categories, tests and parameters in one transaction so the three are consistent."""
        with self._db.transaction() as conn:
            category_ids = [r["category_id"] for r in conn.execute("SELECT category_id FROM categories WHERE active")]
            tests = [CatalogueTest(r["test_id"], r["test_name_en"], r["test_description_en"], tuple(r["category_id"]),
                                   r["active"])
                     for r in conn.execute("SELECT test_id, test_name_en, test_description_en, category_id, active "
                                           "FROM tests")]
            parameters = [CatalogueParameter(r["parameter_id"], tuple(r["category_id"]), r["active"])
                          for r in conn.execute("SELECT parameter_id, category_id, active FROM parameters")]
        return build_catalogue_maps(category_ids, tests, parameters)

    def parameter_categories(self) -> Mapping[int, Collection[int]]:
        return self._load_maps()[0]

    def tests_by_category(self) -> Mapping[int, Sequence[CatalogueTest]]:
        return self._load_maps()[1]

    def test_names_ar(self) -> Mapping[str, str]:
        with self._db.transaction() as conn:
            rows = conn.execute("SELECT test_name_en, test_name_ar FROM tests WHERE test_name_ar <> ''").fetchall()
        return {r["test_name_en"]: r["test_name_ar"] for r in rows}

    def packages(self) -> Sequence[Package]:
        return self._cache("packages", self._read_packages)

    def test_ids_by_name(self) -> Mapping[str, int]:
        return self._cache("test_ids_by_name", self._read_test_ids_by_name)

    def test_names(self) -> Mapping[int, tuple[str, str | None]]:
        return self._cache("test_names", self._read_test_names)

    def _read_packages(self) -> list[Package]:
        with self._db.transaction() as conn:
            rows = conn.execute(
                "SELECT p.package_id, p.package_name_en, p.package_name_ar, p.package_type, p.sex_id, p.age_min, "
                "p.age_max, COALESCE(array_agg(m.test_id) FILTER (WHERE t.active), '{}'::int[]) AS test_ids "
                "FROM packages p LEFT JOIN test_package_map m ON m.package_id = p.package_id "
                "LEFT JOIN tests t ON t.test_id = m.test_id WHERE p.active GROUP BY p.package_id "
                "ORDER BY p.package_id").fetchall()
        return [Package(r["package_id"], r["package_name_en"], r["sex_id"], r["age_min"], r["age_max"],
                        frozenset(r["test_ids"]), r["package_name_ar"], r["package_type"]) for r in rows]

    def _read_test_ids_by_name(self) -> dict[str, int]:
        with self._db.transaction() as conn:
            rows = conn.execute("SELECT test_id, test_name_en, test_name_ar FROM tests WHERE active "
                                "ORDER BY test_id").fetchall()
        by_name: dict[str, int] = {}
        for r in rows:
            for name in (r["test_name_en"], r["test_name_ar"]):      # English and Arabic names
                # the lower test id keeps a name that two tests share
                if name and name.strip():
                    by_name.setdefault(normalize_name(name), r["test_id"])
        return by_name

    def _read_test_names(self) -> dict[int, tuple[str, str | None]]:
        with self._db.transaction() as conn:
            rows = conn.execute("SELECT test_id, test_name_en, test_name_ar FROM tests").fetchall()
        return {r["test_id"]: (r["test_name_en"], r["test_name_ar"] or None) for r in rows}
