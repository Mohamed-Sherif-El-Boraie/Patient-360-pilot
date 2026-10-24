"""In-memory stand-ins for the stores, used by the core flow tests."""
from __future__ import annotations

from patient360.ports import BlobUnavailable, StoredVisit


class FakeCatalogue:
    def __init__(self, parameter_categories=None, tests_by_category=None) -> None:
        self._parameter_categories = parameter_categories or {}
        self._tests_by_category = tests_by_category or {}

    def parameter_categories(self):
        return self._parameter_categories

    def tests_by_category(self):
        return self._tests_by_category


class FakeBlobs:
    def __init__(self, down: bool = False) -> None:
        self.files: dict[str, bytes] = {}
        self.down = down

    def put(self, path: str, data: bytes) -> bool:
        if self.down:
            raise BlobUnavailable
        if path in self.files:
            return False
        self.files[path] = data
        return True


class FakeStore:
    def __init__(self) -> None:
        self.visits: dict[int, dict] = {}
        self.symptom_checks: dict[int, dict] = {}
        self.rejected: list[dict] = []
        self.fail_on_save = False

    def visit_exists(self, visit_id):
        return visit_id in self.visits

    def save_visit(self, payload, raw, idempotency_key, received_at, read_from):
        if self.fail_on_save:
            raise RuntimeError("database down")
        self.visits[payload["visit_id"]] = {"payload": payload, "raw": raw, "idempotency_key": idempotency_key,
                                            "received_at": received_at, "read_from": read_from}

    def stored_visit(self, visit_id):
        visit = self.visits[visit_id]
        return StoredVisit(visit_id, visit["idempotency_key"], visit["payload"]["source"], visit["received_at"],
                           visit["raw"])

    def save_processed(self, visit_id, tier, processed, read_from):
        self.visits[visit_id].update(tier=tier, processed=processed, stage="processed", processed_read_from=read_from)

    def symptom_check_state(self, visit_id, patient_id):
        visit = self.visits.get(visit_id)
        if visit is None or visit["payload"]["patient_id"] != patient_id:
            return "no_visit"
        return "already_used" if visit_id in self.symptom_checks else "ok"

    def save_symptom_check(self, payload, raw, received_at, read_from):
        if self.fail_on_save:
            raise RuntimeError("database down")
        self.symptom_checks[payload["visit_id"]] = {"payload": payload, "raw": raw, "received_at": received_at,
                                                    "read_from": read_from}

    def save_rejected(self, source_id, raw, errors, blob_path):
        self.rejected.append({"source_id": source_id, "raw": raw, "errors": errors, "blob_path": blob_path})
