"""Overview and analytics endpoints (specification §17).

Read-only aggregates for the dashboard. Everything here is computed from the tables
that own the data rather than from stored counters, so the dashboard cannot quietly
disagree with the records it is summarising.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.api.schemas import LeadFunnelResponse, OverviewResponse
from app.persistence.repositories import analytics as analytics_repo

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/overview", response_model=OverviewResponse)
async def get_overview(user: CurrentUser, session: DbSession) -> OverviewResponse:
    """The §17 overview metrics.

    `unread_notifications` is scoped to the caller — notifications are per-user
    ([D3]), so a shared number would be meaningless.
    """
    metrics = await analytics_repo.overview(session, user_id=user.id)
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


@router.get("/leads-by-status", response_model=LeadFunnelResponse)
async def get_lead_funnel(user: CurrentUser, session: DbSession) -> LeadFunnelResponse:
    """Lead counts per state, including the empty ones.

    Gaps would hide where leads are stalling, which is the main thing this view is
    for.
    """
    return LeadFunnelResponse(counts=await analytics_repo.leads_by_status(session))
