"""PostgreSQL implementation of the ``Catalogue`` port: the reference data behind the retrieval rule."""
from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence

from patient360.adapters.postgres.db import Database
from patient360.domain.rules.retrieval import CatalogueParameter, CatalogueTest, build_catalogue_maps


class PostgresCatalogue:
    def __init__(self, db: Database) -> None:
        self._db = db

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
