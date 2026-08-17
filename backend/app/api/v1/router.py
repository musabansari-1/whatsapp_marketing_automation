"""API v1 router (specification §16, base path `/api/v1`).

Phase 1 mounts health only. Later phases add their routers here: auth and users (3),
audiences (4), campaigns (5), webhooks (6), knowledge bases (8), conversations (9),
bookings and notifications (11), analytics (13).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routes import health

api_v1_router = APIRouter()
api_v1_router.include_router(health.router)
