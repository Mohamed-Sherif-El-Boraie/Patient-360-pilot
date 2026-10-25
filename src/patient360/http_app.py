"""The HTTP application: the inbound endpoint for the Backend, the Event Grid webhook for new AWS output files, and
the health check. The flow code is synchronous, so the routes are plain ``def`` (FastAPI runs them in its thread pool);
only the tiny body-reading dependency is async, because Starlette reads a request body asynchronously."""
from __future__ import annotations

import hmac
import json
from typing import Any

import structlog
from fastapi import BackgroundTasks, Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse

from patient360.flow.intake import (
    SYMPTOM_CHECKER,
    Accepted,
    Duplicate,
    Rejected,
    SymptomCheckerClosed,
    TooLarge,
    VisitNotFound,
)
from patient360.services import Services

log = structlog.get_logger()

BATCH = 2
BLOB_CREATED = "Microsoft.Storage.BlobCreated"


def reply(status: int, message: str, code: int = 1, **more: Any) -> JSONResponse:
    """The one place that builds an answer. The ``response.code`` convention (-1 success, 1 failure) copies the
    Backend's style; the exact reply the wrapper expects is not confirmed yet."""
    return JSONResponse({"response": {"code": code, "message": message}, **more}, status_code=status)


class _Stop(Exception):
    """Raised by a dependency to answer at once (wrong key, body too big)."""

    def __init__(self, response: JSONResponse) -> None:
        self.response = response


def _process(services: Services, accepted: Accepted) -> None:
    """After the 202. A failure here is picked up by the 5-minute sweeps, so it must never crash the app."""
    try:
        if accepted.source_id == SYMPTOM_CHECKER:
            services.symptom_processor.process(accepted.visit_id)
            return
        services.processor.process(accepted.visit_id)
        if accepted.source_id == BATCH:
            services.batch_trigger.run()
    except Exception as error:
        log.warning("background_failed", error_type=type(error).__name__)


def create_app(services: Services, lifespan=None) -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    api_key = services.settings.inbound_api_key
    limit = services.settings.max_request_bytes

    @app.exception_handler(_Stop)
    def stop(_: Request, error: _Stop) -> JSONResponse:
        return error.response

    def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
        if api_key is not None and not hmac.compare_digest((x_api_key or "").encode(), api_key.encode()):
            raise _Stop(reply(401, "Unauthorized"))

    async def visit_body(request: Request) -> bytes:
        declared = request.headers.get("content-length", "")
        if limit is not None and declared.isdigit() and int(declared) > limit:
            raise _Stop(reply(413, "Payload too large"))      # answered before the body is read
        return await request.body()

    async def event_body(request: Request) -> bytes:
        return await request.body()

    def accept(background: BackgroundTasks, raw: bytes, symptom_check: bool) -> JSONResponse:
        try:
            outcome = services.intake.receive(raw, symptom_check=symptom_check)
        except Exception as error:
            log.error("request_failed", error_type=type(error).__name__)
            return reply(500, "Internal error")             # no 202 without a safe save: the caller resends
        if isinstance(outcome, Accepted):
            background.add_task(_process, services, outcome)
            return reply(202, "Accepted", -1, data={"visit_id": outcome.visit_id})
        if isinstance(outcome, Duplicate):                   # answered as success so the caller stops resending
            return reply(200, "Duplicate", -1, data={"visit_id": outcome.visit_id})
        if isinstance(outcome, Rejected):
            return reply(422, "Invalid payload", errors=outcome.errors)
        if isinstance(outcome, VisitNotFound):
            return reply(404, "VISIT_NOT_FOUND")
        if isinstance(outcome, SymptomCheckerClosed):
            return reply(409, "SYMPTOM_CHECKER_CLOSED")
        assert isinstance(outcome, TooLarge)
        return reply(413, "Payload too large")

    @app.post("/api/v1/patient360/visits", dependencies=[Depends(require_api_key)])      # from the Backend app service
    def receive_visit(background: BackgroundTasks, raw: bytes = Depends(visit_body)) -> JSONResponse:
        return accept(background, raw, symptom_check=False)

    @app.post("/api/v1/patient360/visits-symptoms", dependencies=[Depends(require_api_key)])      # from the wrapper
    def receive_symptom_check(background: BackgroundTasks, raw: bytes = Depends(visit_body)) -> JSONResponse:
        return accept(background, raw, symptom_check=True)

    @app.post("/api/v1/patient360/blob-events", dependencies=[Depends(require_api_key)])
    def blob_events(raw: bytes = Depends(event_body), aeg_event_type: str | None = Header(default=None)):
        try:
            events = json.loads(raw)
            if aeg_event_type == "SubscriptionValidation":
                return {"validationResponse": events[0]["data"]["validationCode"]}
            paths = [e["subject"].split("/blobs/", 1)[1] for e in events if e["eventType"] == BLOB_CREATED]
        except (ValueError, KeyError, IndexError, TypeError):
            return reply(400, "Invalid payload")
        # Before answering: if it fails we answer 500 and Event Grid retries (it never retries a 200, and a lost event
        # would leave the visit without its result). OutputReceiver is safe to run twice.
        try:
            for path in paths:
                services.receiver.receive(path)
        except Exception as error:
            log.error("request_failed", error_type=type(error).__name__)
            return reply(500, "Internal error")
        return {"received": len(events)}

    @app.get("/health")
    def health() -> JSONResponse:
        try:
            with services.db.transaction() as connection:
                connection.execute("SELECT 1")
        except Exception:
            return JSONResponse({"status": "database_unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    return app
