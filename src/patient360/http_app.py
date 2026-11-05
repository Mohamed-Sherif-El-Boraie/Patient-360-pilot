"""The HTTP application: the inbound endpoints for the Backend, the endpoint the Function app posts each AWS answer
to (``ai-results``), and the health check. The flow code is synchronous, so the routes are plain ``def`` (FastAPI runs
them in its thread pool); only the tiny body-reading dependencies are async, because Starlette reads a body
asynchronously."""
from __future__ import annotations

import hmac
from typing import Any

import structlog
from fastapi import BackgroundTasks, Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse

from patient360.domain.builders.backend_output import build_failed_output
from patient360.domain.common.blob_paths import SOURCES
from patient360.flow.intake import (
    SYMPTOM_CHECKER,
    Accepted,
    Duplicate,
    Rejected,
    Retry,
    SymptomAgain,
    SymptomCheckerClosed,
    TooLarge,
    VisitNotFound,
)
from patient360.services import Services

log = structlog.get_logger()

BATCH = 2
MAX_RESULT_BYTES = 2_000_000


SUCCESS_CODE, ERROR_CODE = -1, 0
MAX_ERROR_ITEMS, MAX_ERROR_CHARS = 5, 300


def reply(status: int, message: str, code: int = ERROR_CODE, **more: Any) -> JSONResponse:
    """The one place that builds an answer, with the two codes the Backend and the wrapper agreed: -1 and the
    message "success" (plus ``data``) when we received the data and answer with data; 0 and only a message when we
    received it but cannot accept it (visit not found, invalid payload, ...). There is no other code."""
    return JSONResponse({"response": {"code": code, "message": message}, **more}, status_code=status)


def invalid_message(errors: list[dict[str, Any]]) -> str:
    """One readable line for the 422, so the answer keeps to code and message: where each problem is and what it is.
    The errors never hold the input values (see ``validate_payload``)."""
    shown = [f"{'.'.join(e['loc']) or 'body'}: {e['msg']}" for e in errors[:MAX_ERROR_ITEMS]]
    more = f" (+{len(errors) - MAX_ERROR_ITEMS} more)" if len(errors) > MAX_ERROR_ITEMS else ""
    return f"Invalid payload: {'; '.join(shown)}{more}"[:MAX_ERROR_CHARS]


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


def _tell_backend_rejected(services: Services, rejected: Rejected) -> None:
    """After the 422: the payload is in Blob 1 ``_rejected``, so tell the Backend this patient failed. Best effort: no
    visit row exists, so the delivery queue cannot hold it and nothing is retried."""
    if services.backend is None:
        log.info("rejected_failed_message_skipped", reason="backend_not_configured")
        return
    try:
        services.backend.post_result(build_failed_output(
            patient_id=rejected.patient_id, visit_id=rejected.visit_id, source=rejected.source_id,
            company_id=rejected.company_id))
    except Exception as error:
        log.warning("rejected_failed_message_not_sent", error_type=type(error).__name__)


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

    async def result_body(request: Request) -> bytes:
        """One AI answer is a few KB: a bigger body is refused, also when it arrives in chunks."""
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_RESULT_BYTES:
            raise _Stop(reply(413, "Payload too large"))
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_RESULT_BYTES:
                raise _Stop(reply(413, "Payload too large"))
        return body

    def accept(background: BackgroundTasks, raw: bytes, symptom_check: bool) -> JSONResponse:
        try:
            outcome = services.intake.receive(raw, symptom_check=symptom_check)
        except Exception as error:
            log.error("request_failed", error_type=type(error).__name__)
            return reply(500, "Internal error")             # no 202 without a safe save: the caller resends
        if isinstance(outcome, Accepted):
            background.add_task(_process, services, outcome)
            return reply(202, "success", SUCCESS_CODE, data={"visit_id": outcome.visit_id})
        if isinstance(outcome, Retry):                       # we have it, its result was not delivered: sent again
            return reply(200, "success", SUCCESS_CODE, data={"visit_id": outcome.visit_id})
        if isinstance(outcome, Duplicate):                   # we have it and its result was delivered
            return reply(409, "DUPLICATE_VISIT")
        if isinstance(outcome, SymptomAgain):                # started again by the wrapper: the checker is closed now
            return reply(200, "success", SUCCESS_CODE, data=outcome.data)
        if isinstance(outcome, Rejected):
            if None not in (outcome.patient_id, outcome.visit_id, outcome.source_id):
                background.add_task(_tell_backend_rejected, services, outcome)
            return reply(422, invalid_message(outcome.errors))
        if isinstance(outcome, VisitNotFound):
            return reply(404, "VISIT_NOT_FOUND")
        if isinstance(outcome, SymptomCheckerClosed):
            return reply(409, "SYMPTOM_CHECKER_CLOSED")
        assert isinstance(outcome, TooLarge)
        return reply(413, "Payload too large")

    # The two visit routes carry no key: the Backend and the wrapper are protected by the network (App Service access
    # restrictions). Only the Function app, which we own, sends the key (see /ai-results).
    @app.post("/api/v1/patient360/visits")      # from the Backend app service
    def receive_visit(background: BackgroundTasks, raw: bytes = Depends(visit_body)) -> JSONResponse:
        return accept(background, raw, symptom_check=False)

    @app.post("/api/v1/patient360/visits-symptoms")      # from the wrapper
    def receive_symptom_check(background: BackgroundTasks, raw: bytes = Depends(visit_body)) -> JSONResponse:
        return accept(background, raw, symptom_check=True)

    @app.post("/api/v1/patient360/ai-results", dependencies=[Depends(require_api_key)])      # from the Function app
    def receive_ai_result(
        raw: bytes = Depends(result_body), x_source: str | None = Header(default=None),
        x_blob_path: str | None = Header(default=None), idempotency_key: str | None = Header(default=None),
        x_visit_key: str | None = Header(default=None),
    ) -> JSONResponse:
        if x_source not in SOURCES or not x_blob_path:
            return reply(400, "Invalid headers")
        log.info("ai_result_received", source=x_source, has_idempotency_key=idempotency_key is not None)
        # Answered 200 for everything a retry cannot change; 500 only when it is worth retrying (the Function retries
        # a 5xx). OutputReceiver is safe to run twice. Outcomes: saved, blocked, duplicate, unknown_visit,
        # source_mismatch, invalid_output, ignored. A failed line of a batch with no usable visit key is a permanent
        # 400 (a retry can not help: the Function sets it aside). ``X-Visit-Key``: the visit of that line.
        try:
            results = services.receiver.receive_record(x_source, x_blob_path, raw.decode(errors="replace"),
                                                       x_visit_key)
        except Exception as error:
            log.error("request_failed", error_type=type(error).__name__)
            return reply(500, "Internal error")
        if results[0].outcome == "unreadable":
            return reply(400, "Failed line without a visit key")
        return JSONResponse({"outcome": results[0].outcome})

    @app.get("/health")
    def health() -> JSONResponse:
        try:
            with services.db.transaction() as connection:
                connection.execute("SELECT 1")
        except Exception:
            return JSONResponse({"status": "database_unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    return app
