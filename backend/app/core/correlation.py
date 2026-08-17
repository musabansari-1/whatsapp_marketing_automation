"""Correlation ID and log-context propagation.

Specification §21 requires structured logs carrying a correlation ID plus the
request/task/lead/campaign/conversation/booking identifiers. Those identifiers are
bound to structlog's contextvars, so every log line emitted downstream of the bind
carries them without being threaded through function signatures.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

import structlog

CORRELATION_ID_HEADER = "X-Correlation-ID"

# Identifiers the specification asks us to be able to trace by (§21).
TRACEABLE_FIELDS = (
    "request_id",
    "task_id",
    "lead_id",
    "campaign_id",
    "conversation_id",
    "booking_id",
)

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def set_correlation_id(value: str | None = None) -> str:
    """Set the ambient correlation ID, generating one when absent.

    Returns the effective ID so callers can echo it back on a response.
    """
    correlation_id = value or new_correlation_id()
    _correlation_id.set(correlation_id)
    structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
    return correlation_id


def clear_correlation_id() -> None:
    _correlation_id.set(None)
    structlog.contextvars.unbind_contextvars("correlation_id")


def bind_log_context(**fields: object) -> None:
    """Bind additional identifiers to the ambient log context.

    Unknown keys are permitted, but the §21 identifiers are the ones the platform
    is expected to be traceable by.
    """
    cleaned = {key: value for key, value in fields.items() if value is not None}
    if cleaned:
        structlog.contextvars.bind_contextvars(**cleaned)


@contextmanager
def correlation_scope(value: str | None = None, **fields: object) -> Iterator[str]:
    """Run a block with a fresh, isolated log context.

    Used by the worker so each task execution gets its own correlation scope and
    cannot leak identifiers into the next task on the same process.
    """
    previous_bindings = structlog.contextvars.get_contextvars()
    previous_correlation_id = _correlation_id.get()

    structlog.contextvars.clear_contextvars()
    correlation_id = set_correlation_id(value)
    bind_log_context(**fields)
    try:
        yield correlation_id
    finally:
        structlog.contextvars.clear_contextvars()
        _correlation_id.set(previous_correlation_id)
        if previous_bindings:
            structlog.contextvars.bind_contextvars(**previous_bindings)
