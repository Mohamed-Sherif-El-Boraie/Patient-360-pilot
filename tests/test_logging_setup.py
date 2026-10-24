import io
import json

import pytest
import structlog

from patient360.logging_setup import configure_logging

CANARIES = {"patient_id": 7000007, "visit_id": 9000009, "patient_symptoms": "CANARY-SYMPTOM-TEXT",
            "patient_name": "CANARY-NAME", "value": "CANARY-VALUE"}


@pytest.fixture(autouse=True)
def clean_logging():
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()
    yield
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


def lines(level, log_format, write):
    stream = io.StringIO()
    configure_logging(level, log_format, stream)
    write(structlog.get_logger())
    return stream.getvalue()


@pytest.mark.parametrize("level", ["DEBUG", "INFO"])
@pytest.mark.parametrize("log_format", ["json", "console"])
def test_patient_data_never_reaches_the_log(level, log_format):
    out = lines(level, log_format, lambda log: log.info("saved", stage="processed", **CANARIES))
    assert "saved" in out and "processed" in out
    assert not any(str(value) in out for value in CANARIES.values())
    assert not any(name in out for name in CANARIES)


def test_the_allowed_fields_are_written_as_json_with_a_utc_time():
    out = lines("INFO", "json", lambda log: log.info("sent", idempotency_key="abc123", source="urgent",
                                                      duration_ms=412, status="delivered"))
    entry = json.loads(out)
    assert (entry["event"], entry["level"]) == ("sent", "info")
    assert (entry["idempotency_key"], entry["duration_ms"]) == ("abc123", 412)
    assert entry["timestamp"].endswith("Z")


def test_debug_lines_appear_only_at_debug_level():
    def write(log):
        log.debug("detail")
        log.info("normal")
    assert "detail" not in lines("INFO", "json", write) and "normal" in lines("INFO", "json", write)
    assert "detail" in lines("DEBUG", "json", write)


def test_a_warning_level_hides_info_and_shows_errors():
    def write(log):
        log.info("info line")
        log.error("error line")
    out = lines("WARNING", "json", write)
    assert "info line" not in out and "error line" in out


def test_an_exception_text_is_not_logged():
    def write(log):
        try:
            raise RuntimeError("CANARY-EXCEPTION-TEXT with patient data")
        except RuntimeError as exc:
            log.error("failed", error_type=type(exc).__name__, exc_info=True)
    out = lines("DEBUG", "json", write)
    assert "RuntimeError" in out and "CANARY-EXCEPTION-TEXT" not in out


def test_the_bound_key_follows_the_record_until_it_is_cleared():
    stream = io.StringIO()
    configure_logging("INFO", "json", stream)
    log = structlog.get_logger()
    structlog.contextvars.bind_contextvars(idempotency_key="key-1", source="urgent")
    log.info("first")
    structlog.contextvars.clear_contextvars()
    log.info("second")
    first, second = (json.loads(line) for line in stream.getvalue().splitlines())
    assert first["idempotency_key"] == "key-1" and first["source"] == "urgent"
    assert "idempotency_key" not in second


def test_a_bound_field_that_is_not_allowed_is_dropped_too():
    stream = io.StringIO()
    configure_logging("INFO", "json", stream)
    structlog.contextvars.bind_contextvars(patient_id=7000007)
    structlog.get_logger().info("x")
    assert "7000007" not in stream.getvalue()


def test_a_wrong_level_or_format_is_refused():
    with pytest.raises(ValueError):
        configure_logging("TRACE", "json")
    with pytest.raises(ValueError):
        configure_logging("INFO", "xml")
