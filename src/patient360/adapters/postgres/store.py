"""The PostgreSQL store: the three parts that implement ``VisitStore``, put together."""
from __future__ import annotations

from patient360.adapters.postgres.catalogue import PostgresCatalogue
from patient360.adapters.postgres.db import Database
from patient360.adapters.postgres.outputs import OutputsMixin
from patient360.adapters.postgres.visits import VisitsMixin
from patient360.adapters.postgres.work import WorkMixin


class PostgresStore(VisitsMixin, WorkMixin, OutputsMixin):
    """Implements ``VisitStore``: visits and intake (visits.py), the work queue and batching (work.py), the AI outputs
    and their delivery (outputs.py)."""

    def __init__(self, db: Database) -> None:
        self._db = db


__all__ = ["PostgresCatalogue", "PostgresStore"]
