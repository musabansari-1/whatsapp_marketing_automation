"""API v1 router (specification §16, base path `/api/v1`).

Every §16 route group is registered here.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routes import (
    analytics,
    audiences,
    auth,
    campaigns,
    conversations,
    health,
    knowledge,
    notifications,
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
api_v1_router.include_router(conversations.router)
api_v1_router.include_router(notifications.router)
api_v1_router.include_router(analytics.router)
