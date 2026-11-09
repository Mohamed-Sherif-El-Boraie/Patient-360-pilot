"""Where an internal failure happened, so an error can be traced and not only counted.

A failure is told by three things: the step it happened in ("saving the visit in the database"), the exception class
with its module ("psycopg_pool.PoolTimeout") and the place in our code (file, line, function). The exception message
is never used: a database or HTTP message can hold values of the payload (patient data)."""
from __future__ import annotations

import os
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


class StepFailed(Exception):
    """An unexpected error inside one named step of a request. ``error`` is the original exception."""

    def __init__(self, step: str, error: BaseException, visit_id: int | None = None) -> None:
        super().__init__(step)
        self.step, self.error, self.visit_id = step, error, visit_id


@contextmanager
def step(what: str, visit_id: int | None = None) -> Iterator[None]:
    """Name the step: any unexpected error inside becomes a ``StepFailed`` that says where it happened."""
    try:
        yield
    except StepFailed:
        raise
    except Exception as error:
        raise StepFailed(what, error, visit_id) from error


def error_name(error: BaseException) -> str:
    kind = type(error)
    return kind.__name__ if kind.__module__ == "builtins" else f"{kind.__module__}.{kind.__name__}"


def where(error: BaseException) -> str:
    """The last place in the traceback: ``intake.py line 142 in receive``."""
    frames = traceback.extract_tb(error.__traceback__)
    if not frames:
        return "unknown"
    last = frames[-1]
    return f"{os.path.basename(last.filename)} line {last.lineno} in {last.name}"


def report(log: Any, what: str, error: BaseException, visit_id: int | None = None, detail: str | None = None) -> str:
    """Log the failure (step, exception class, place in the code, visit) and return the message for the answer:
    ``Internal error while saving the visit in the database: psycopg_pool.PoolTimeout (visit_id 123)``."""
    if isinstance(error, StepFailed):
        what, visit_id, error = error.step, error.visit_id, error.error
    log.error("request_failed", step=what, error_type=error_name(error), where=where(error), visit_id=visit_id,
              detail=detail)
    parts = ([f"visit_id {visit_id}"] if visit_id is not None else []) + ([detail] if detail else [])
    return f"Internal error while {what}: {error_name(error)}" + (f" ({', '.join(parts)})" if parts else "")
