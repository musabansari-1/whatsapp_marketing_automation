"""Structured logging setup (specification §21).

Both services log JSON to stdout in deployed environments so Cloud Logging can
parse the fields. Standard-library loggers (uvicorn, sqlalchemy) are routed through
the same structlog pipeline, so third-party log lines are structured too rather
than appearing as unparsed text alongside our own.

PII: §19 asks us to avoid unnecessary PII logging. Log identifiers, not message
bodies or phone numbers.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from app.core.config import Settings

_configured = False


def _shared_processors() -> list[Any]:
    """Processors applied to structlog and stdlib records alike."""
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]


def configure_logging(settings: Settings, *, force: bool = False) -> None:
    """Configure structlog and the stdlib root logger.

    Idempotent unless `force` is passed, so importing both service entrypoints in
    one test session does not stack handlers.
    """
    global _configured
    if _configured and not force:
        return

    shared = _shared_processors()

    renderer: Any
    if settings.log_format == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=False)

    structlog.configure(
        processors=[
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        # False so reconfiguration inside tests takes effect immediately.
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)

    # uvicorn installs its own handlers; drop them so records propagate to root
    # and are rendered once, by us.
    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(noisy)
        logger.handlers = []
        logger.propagate = True

    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
