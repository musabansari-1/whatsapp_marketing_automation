"""Liveness and readiness endpoints.

Cloud Run health checks and the Phase 15 smoke tests use these. Liveness answers
"is the process up"; readiness answers "can it serve traffic", which means checking
the database.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from app.core.config import Settings, get_settings
from app.persistence.database import check_database_connection

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str
    environment: str


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, bool]


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness. Deliberately dependency-free so it stays up when a dependency is down."""
    settings: Settings = get_settings()
    return HealthResponse(service="api", environment=settings.app_env.value)


@router.get("/readiness", response_model=ReadinessResponse)
async def readiness(response: Response) -> ReadinessResponse:
    """Readiness. Returns 503 when a dependency is unavailable."""
    database_ok = await check_database_connection()
    checks = {"database": database_ok}

    if not all(checks.values()):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessResponse(status="not_ready", checks=checks)

    return ReadinessResponse(status="ready", checks=checks)
