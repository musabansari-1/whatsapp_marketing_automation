"""Version 1 API router.

Two kinds of route, and the difference is structural rather than a naming convention:

- **Global** routes serve the organisation as a whole: health, authentication, users,
  webhooks, the product list, notifications, unassigned templates, combined
  analytics and the token-authenticated integrations endpoint.
- **Product-scoped** routes live under `/p/{product_slug}` and cannot be reached without
  naming a product (§28, Phase 26).

`tests/test_route_scope.py` classifies every route and fails on one that is neither — so
a list endpoint added later cannot quietly return every product's rows.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routes import (
    analytics,
    audiences,
    auth,
    bookings,
    campaigns,
    conversations,
    health,
    integrations,
    knowledge,
    notifications,
    products,
    templates,
    users,
    webhooks,
)

api_v1_router = APIRouter()

# ---------------------------------------------------------------- global
api_v1_router.include_router(health.router)
api_v1_router.include_router(auth.router)
api_v1_router.include_router(users.router)
api_v1_router.include_router(webhooks.router)
api_v1_router.include_router(products.router)
api_v1_router.include_router(notifications.router)
api_v1_router.include_router(templates.global_router)
api_v1_router.include_router(analytics.global_router)
api_v1_router.include_router(integrations.router)

# ---------------------------------------------------------------- product-scoped
api_v1_router.include_router(products.scoped_router)
api_v1_router.include_router(audiences.router)
api_v1_router.include_router(campaigns.router)
api_v1_router.include_router(templates.router)
api_v1_router.include_router(templates.first_reply_router)
api_v1_router.include_router(conversations.router)
api_v1_router.include_router(bookings.router)
api_v1_router.include_router(knowledge.router)
api_v1_router.include_router(analytics.router)
