"""Worker service entrypoint (Cloud Run service 2 of 2).

Stateless (§18). Cloud Tasks delivers work over HTTP; this service executes it.

Phase 1 establishes the service, its health probes and the dispatch route. Phase 7
adds Cloud Tasks OIDC verification, retry semantics and idempotency bookkeeping —
until then the dispatch route is reachable only in local development and tests, and
no handlers are registered.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.integrations.queue import TaskRequest, TaskType
from app.persistence.database import check_database_connection, dispose_engine
from app.worker.dispatcher import dispatch_task
from app.worker.registry import UnknownTaskTypeError, task_registry

logger = get_logger(__name__)


class TaskEnvelope(BaseModel):
    """The HTTP body Cloud Tasks delivers."""

    task_type: TaskType
    task_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = None

    def to_request(self) -> TaskRequest:
        return TaskRequest(
            task_type=self.task_type,
            payload=self.payload,
            task_id=self.task_id,
            correlation_id=self.correlation_id,
        )


class TaskResult(BaseModel):
    status: Literal["completed"] = "completed"
    task_id: str
    task_type: TaskType


class WorkerHealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str = "worker"
    environment: str
    registered_task_types: list[str]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = get_settings()
    logger.info(
        "worker_starting",
        environment=settings.app_env.value,
        registered_task_types=[t.value for t in task_registry.registered_types()],
    )
    yield
    await dispose_engine()
    logger.info("worker_stopped")


def create_worker_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title="WhatsApp AI Marketing Platform Worker",
        version="0.1.0",
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
        lifespan=lifespan,
    )

    # Imported here to avoid a circular import with the API middleware module.
    from app.api.middleware import CorrelationIdMiddleware, RequestLoggingMiddleware

    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/health", response_model=WorkerHealthResponse)
    async def health() -> WorkerHealthResponse:
        return WorkerHealthResponse(
            environment=settings.app_env.value,
            registered_task_types=[t.value for t in task_registry.registered_types()],
        )

    @app.get("/readiness")
    async def readiness() -> dict[str, object]:
        database_ok = await check_database_connection()
        if not database_ok:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"status": "not_ready", "checks": {"database": False}},
            )
        return {"status": "ready", "checks": {"database": True}}

    @app.post("/internal/tasks", response_model=TaskResult)
    async def execute_task(envelope: TaskEnvelope) -> TaskResult:
        """Execute one task.

        A task with no registered handler returns 400, not 500: retrying it would
        never succeed, so Cloud Tasks must not keep redelivering it.
        """
        try:
            await dispatch_task(envelope.to_request())
        except UnknownTaskTypeError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return TaskResult(task_id=envelope.task_id, task_type=envelope.task_type)

    return app


app = create_worker_app()
