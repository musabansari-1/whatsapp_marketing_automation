"""API service entrypoint (Cloud Run service 1 of 2).

Serves the dashboard API behind the Next.js BFF proxy, plus the WhatsApp webhook,
which Meta calls directly and which therefore bypasses the proxy ([D13]).

Long-running work never happens here (§4): request handlers persist and enqueue.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.middleware import CorrelationIdMiddleware, RequestLoggingMiddleware
from app.api.v1.router import api_v1_router
from app.api.v1.routes.health import router as health_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.persistence.database import dispose_engine

API_V1_PREFIX = "/api/v1"

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = get_settings()
    logger.info(
        "api_starting",
        environment=settings.app_env.value,
        whatsapp_provider=settings.whatsapp_provider.value,
        llm_provider=settings.llm_provider.value,
        embedding_provider=settings.embedding_provider.value,
        calendar_provider=settings.calendar_provider.value,
        crm_provider=settings.crm_provider.value,
        queue_provider=settings.queue_provider.value,
    )
    yield
    await dispose_engine()
    logger.info("api_stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title="WhatsApp AI Marketing Platform API",
        version="0.1.0",
        # Docs stay off in production: this is an internal tool and §19 says to
        # protect internal APIs.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
        lifespan=lifespan,
    )

    # Starlette wraps in reverse registration order, so the last added is outermost.
    # Correlation must therefore be added last: it binds the ID before request
    # logging runs, so every line carries it.
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(CorrelationIdMiddleware)

    # No CORS middleware by design. [D13] routes browser traffic through the
    # same-origin BFF, so the browser never issues a cross-origin request to this
    # service and no permissive CORS policy is needed.

    app.include_router(api_v1_router, prefix=API_V1_PREFIX)

    # Unprefixed probes for Cloud Run.
    app.include_router(health_router)

    return app


app = create_app()
