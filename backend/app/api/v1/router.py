"""API v1 router (specification §16, base path `/api/v1`).

Later phases add their routers here: conversations (9), bookings and
notifications (11), analytics (13).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routes import (
    audiences,
    auth,
    campaigns,
    health,
    knowledge,
    users,
    webhooks,
)

api_v1_router = APIRouter()
api_v1_router.include_router(health.router)
api_v1_router.include_router(auth.router)
api_v1_router.include_router(users.router)
api_v1_router.include_router(audiences.router)
api_v1_router.include_router(campaigns.router)
api_v1_router.include_router(webhooks.router)
api_v1_router.include_router(knowledge.router)
