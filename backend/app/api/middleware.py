"""HTTP middleware for correlation and request logging (specification §21)."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.correlation import (
    CORRELATION_ID_HEADER,
    bind_log_context,
    clear_correlation_id,
    set_correlation_id,
)
from app.core.logging import get_logger

logger = get_logger(__name__)

NextCall = Callable[[Request], Awaitable[Response]]


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Adopt or mint a correlation ID for every request.

    The BFF forwards its correlation ID ([D13]), so a browser request keeps one
    identifier across the proxy hop into the API. The ID is echoed on the response
    so clients and logs can be tied together.
    """

    async def dispatch(self, request: Request, call_next: NextCall) -> Response:
        incoming = request.headers.get(CORRELATION_ID_HEADER)
        correlation_id = set_correlation_id(incoming)
        bind_log_context(request_id=correlation_id)
        try:
            response = await call_next(request)
            response.headers[CORRELATION_ID_HEADER] = correlation_id
            return response
        finally:
            # Cleared even on failure so the ID cannot leak into another request
            # handled by the same worker task.
            clear_correlation_id()


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log one structured line per request.

    Logs method, path, status and duration only. §19 asks us to avoid unnecessary PII
    logging, so query strings and bodies are deliberately not recorded.
    """

    async def dispatch(self, request: Request, call_next: NextCall) -> Response:
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.exception(
                "request_failed",
                http_method=request.method,
                path=request.url.path,
                duration_ms=duration_ms,
            )
            raise

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info(
            "request_completed",
            http_method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        return response
