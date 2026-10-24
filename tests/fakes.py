"""In-memory stand-ins for the stores, used by the core flow tests."""
from __future__ import annotations

from datetime import UTC, datetime

from patient360.domain.builders.ai_input import SOURCE_NAMES
from patient360.domain.common.ids import format_batch_id
from patient360.domain.rules.classify import LANE_BY_TIER
from patient360.domain.rules.dispatch import SOURCE_PRIORITY
from patient360.ports import (
    BatchVisit,
    BlobUnavailable,
    ClaimedBatch,
    DeliveryUnit,
    S3Unavailable,
    StoredVisit,
    WorkUnit,
)


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

    def get(self, path: str) -> bytes | None:
        if self.down:
            raise BlobUnavailable
        return self.files.get(path)


class FakeS3:
    def __init__(self, down: bool = False) -> None:
        self.files: dict[tuple[str, str], bytes] = {}      # (source, key) -> bytes
        self.uploads: list[tuple[str, str]] = []           # in the order they arrived
        self.down = down

    def put(self, source: str, key: str, data: bytes) -> None:
        if self.down:
            raise S3Unavailable
        self.files[(source, key)] = data
        self.uploads.append((source, key))


class FakeBackend:
    """Stands in for the Backend results endpoint. Set ``raises`` to an exception class to make every call fail."""

    def __init__(self) -> None:
        self.posts: list[dict] = []
        self.raises: type[Exception] | None = None

    def post_result(self, body: dict) -> None:
        if self.raises:
            raise self.raises
        self.posts.append(body)


class FakeStore:
    def __init__(self) -> None:
        self.visits: dict[int, dict] = {}
        self.symptom_checks: dict[int, dict] = {}
        self.batches: dict[str, dict] = {}
        self.rejected: list[dict] = []
        self.fail_on_save = False
        self.batch_numbers = {"normal": 0, "critical": 0}
        self.batch_created_at = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)   # the database time of a new batch

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

    def save_processed(self, visit_id, tier, model_input, read_from):
        self.visits[visit_id].update(tier=tier, model_input=model_input, stage="processed",
                                     processed_read_from=read_from)

    def claim_batch(self, lane, size):
        waiting = sorted((v for v in self.visits.values()
                          if v["payload"]["source"] == 2 and v.get("stage") == "processed" and "batch_id" not in v
                          and LANE_BY_TIER[v["tier"]] == lane),
                         key=lambda v: (v["received_at"], v["payload"]["visit_id"]))
        if len(waiting) < size:
            return None
        self.batch_numbers[lane] += 1
        batch_id = format_batch_id(lane, self.batch_numbers[lane])
        for visit in waiting[:size]:
            visit.update(batch_id=batch_id, stage="queued")
        self.batches[batch_id] = {"lane": lane, "created_at": self.batch_created_at, "status": "created",
                                  "claimed": False, "attempts": 0, "read_from": "postgresql",
                                  "visit_ids": [v["payload"]["visit_id"] for v in waiting[:size]]}
        visits = [BatchVisit(v["idempotency_key"], v["model_input"]) for v in waiting[:size]]
        return ClaimedBatch(batch_id, visits, self.batch_created_at)

    def set_batch_read_from(self, batch_id, read_from):
        self.batches[batch_id]["read_from"] = read_from

    # ---- the dispatcher ---------------------------------------------------------------------
    def add_symptom_check_ready(self, visit_id, model_input, read_from="postgresql"):
        """Stand-in for the symptom flow (a later task): a symptom check whose request is ready to upload."""
        self.symptom_checks.setdefault(visit_id, {}).update(
            stage="processed_symptom", model_input=model_input, processed_read_from=read_from,
            received_at=self.visits[visit_id]["received_at"])

    def _work(self):
        """Every unit that could be uploaded: (priority, created_at, id, record, unit)."""
        for visit_id, v in self.visits.items():
            name = SOURCE_NAMES[v["payload"]["source"]]
            if name != "batch" and v.get("stage") in ("processed", "queued"):
                unit = WorkUnit(name, LANE_BY_TIER[v["tier"]], v["received_at"], v["processed_read_from"],
                                v.get("attempts", 0), visit_id=visit_id, idempotency_key=v["idempotency_key"])
                yield SOURCE_PRIORITY[name], v["received_at"], visit_id, v, unit
        for visit_id, s in self.symptom_checks.items():
            if s.get("stage") in ("processed_symptom", "queued_symptom"):
                v = self.visits[visit_id]
                unit = WorkUnit("symptom-checker", LANE_BY_TIER[v["tier"]], s["received_at"], s["processed_read_from"],
                                s.get("attempts", 0), visit_id=visit_id, idempotency_key=v["idempotency_key"],
                                analysis="symptom")
                yield SOURCE_PRIORITY["symptom-checker"], s["received_at"], visit_id, s, unit
        for batch_id, b in self.batches.items():
            if b["status"] == "created":
                unit = WorkUnit("batch", b["lane"], b["created_at"], b["read_from"], b["attempts"], batch_id=batch_id)
                yield SOURCE_PRIORITY["batch"], b["created_at"], batch_id, b, unit

    def claim_next_work(self, now):
        ready = [item for item in self._work()
                 if not item[3].get("claimed") and (item[3].get("next_retry_at") or now) <= now]
        if not ready:
            return None
        _, _, _, record, unit = min(ready, key=lambda item: item[:2])
        record["claimed"] = True
        if "stage" in record:
            record["stage"] = "queued_symptom" if unit.analysis == "symptom" else "queued"
        return unit

    def _record(self, unit):
        if unit.batch_id:
            return self.batches[unit.batch_id]
        return (self.symptom_checks if unit.analysis == "symptom" else self.visits)[unit.visit_id]

    def model_input_of(self, unit):
        return self._record(unit)["model_input"]

    def batch_model_inputs(self, batch_id):
        return [BatchVisit(self.visits[i]["idempotency_key"], self.visits[i]["model_input"])
                for i in self.batches[batch_id]["visit_ids"]]

    def _set_stage(self, unit, stage):
        if unit.batch_id:
            for visit_id in self.batches[unit.batch_id]["visit_ids"]:
                self.visits[visit_id]["stage"] = stage
        else:
            self._record(unit)["stage"] = stage + ("_symptom" if unit.analysis == "symptom" else "")

    def mark_sent(self, unit):
        record = self._record(unit)
        record["claimed"] = False
        if unit.batch_id:
            record["status"] = "submitted"
        self._set_stage(unit, "sent_to_aws")

    def mark_failed_attempt(self, unit, error_type, retry_at):
        record = self._record(unit)
        record.update(claimed=False, attempts=record.get("attempts", 0) + 1, last_error=error_type,
                      next_retry_at=retry_at)
        if retry_at is None:
            if unit.batch_id:
                record["status"] = "failed"
            self._set_stage(unit, "failed")

    # ---- sending the result to the Backend ------------------------------------------------------
    def add_result_ready(self, visit_id, body, analysis="visit"):
        """Stand-in for the AWS output flow (a later task): a result whose public JSON is ready to send."""
        record = self.symptom_checks[visit_id] if analysis == "symptom" else self.visits[visit_id]
        record.update(public_output=body, stage="post_processing_symptom" if analysis == "symptom"
                      else "post_processing")

    def _deliveries(self):
        for visit_id, v in self.visits.items():
            if v.get("stage") == "post_processing":
                name = SOURCE_NAMES[v["payload"]["source"]]
                yield SOURCE_PRIORITY[name], v["received_at"], v, DeliveryUnit(
                    visit_id, name, v.get("delivery_attempts", 0))
        for visit_id, s in self.symptom_checks.items():
            if s.get("stage") == "post_processing_symptom":
                yield SOURCE_PRIORITY["symptom-checker"], s["received_at"], s, DeliveryUnit(
                    visit_id, "symptom-checker", s.get("delivery_attempts", 0), "symptom")

    def claim_next_delivery(self, now):
        ready = [item for item in self._deliveries()
                 if not item[2].get("delivery_claimed") and (item[2].get("delivery_retry_at") or now) <= now]
        if not ready:
            return None
        _, _, record, unit = min(ready, key=lambda item: item[:2])
        record["delivery_claimed"] = True
        return unit

    def _delivery_record(self, unit):
        return (self.symptom_checks if unit.analysis == "symptom" else self.visits)[unit.visit_id]

    def public_output_of(self, unit):
        return self._delivery_record(unit)["public_output"]

    def mark_delivered(self, unit):
        record = self._delivery_record(unit)
        record.update(delivery_claimed=False, stage="delivered_symptom" if unit.analysis == "symptom" else "delivered")

    def mark_delivery_failed_attempt(self, unit, error_type, retry_at):
        record = self._delivery_record(unit)
        record.update(delivery_claimed=False, delivery_attempts=record.get("delivery_attempts", 0) + 1,
                      delivery_error=error_type, delivery_retry_at=retry_at)
        if retry_at is None:
            record["stage"] = "failed_symptom" if unit.analysis == "symptom" else "failed"

    # ---- the symptom check and the rejected payloads ------------------------------------------
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
