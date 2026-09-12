"""Overview and analytics endpoints (specification §17, [D53]).

Read-only aggregates, computed from the tables that own the data rather than from stored
counters, so the dashboard cannot quietly disagree with the records it summarises.

[D53] asks for three views from one query architecture, and that is what this is — one
repository, three scopes:

    /p/{product}/analytics/…                  one product
    /p/{product}/analytics/…?segment=slug     one segment within it
    /analytics/…                              every product, combined

The combined view is its own address. It is never what a product view falls back to when
a product is omitted — a product view cannot omit its product.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.api.schemas import LeadFunnelResponse, OverviewResponse
from app.api.scope import PRODUCT_PREFIX, ProductScope, SegmentFilter
from app.persistence.repositories import analytics as analytics_repo
from app.persistence.repositories.analytics import AnalyticsScope, OverviewMetrics

router = APIRouter(prefix=f"{PRODUCT_PREFIX}/analytics", tags=["analytics"])
global_router = APIRouter(prefix="/analytics", tags=["analytics"])


def _response(metrics: OverviewMetrics) -> OverviewResponse:
    return OverviewResponse(
        campaigns=metrics.campaigns,
        campaigns_sending=metrics.campaigns_sending,
        messages_sent=metrics.messages_sent,
        delivered=metrics.delivered,
        read=metrics.read,
        failed=metrics.failed,
        replies=metrics.replies,
        total_leads=metrics.total_leads,
        interested=metrics.interested,
        qualified=metrics.qualified,
        demos_booked=metrics.demos_booked,
        unread_notifications=metrics.unread_notifications,
    )


@router.get("/overview", response_model=OverviewResponse)
async def get_product_overview(
    product: ProductScope, segment: SegmentFilter, user: CurrentUser, session: DbSession
) -> OverviewResponse:
    """The §17 overview for one product, optionally one segment of it.

    `unread_notifications` is the caller's own, for this product — notifications are
    per-user ([D3]) and carry a product but no segment ([D47]).
    """
    scope = AnalyticsScope.for_product(
        product.id, segment_id=segment.id if segment is not None else None
    )
    return _response(await analytics_repo.overview(session, scope=scope, user_id=user.id))


@router.get("/leads-by-status", response_model=LeadFunnelResponse)
async def get_product_lead_funnel(
    product: ProductScope, segment: SegmentFilter, session: DbSession
) -> LeadFunnelResponse:
    """Lead counts per state for one product, including the empty states."""
    scope = AnalyticsScope.for_product(
        product.id, segment_id=segment.id if segment is not None else None
    )
    return LeadFunnelResponse(counts=await analytics_repo.leads_by_status(session, scope=scope))


@global_router.get("/overview", response_model=OverviewResponse)
async def get_combined_overview(user: CurrentUser, session: DbSession) -> OverviewResponse:
    """The §17 overview across every product, combined ([D53]).

    Asked for explicitly. `unread_notifications` is every unread notification this user
    has.
    """
    return _response(
        await analytics_repo.overview(session, scope=AnalyticsScope.all_products(), user_id=user.id)
    )


@global_router.get("/leads-by-status", response_model=LeadFunnelResponse)
async def get_combined_lead_funnel(user: CurrentUser, session: DbSession) -> LeadFunnelResponse:
    """Lead counts per state across every product.

    A person who is a lead in two products counts twice here, because they are two leads
    with two independent states ([D41]).
    """
    return LeadFunnelResponse(
        counts=await analytics_repo.leads_by_status(session, scope=AnalyticsScope.all_products())
    )


__all__ = ["global_router", "router"]
