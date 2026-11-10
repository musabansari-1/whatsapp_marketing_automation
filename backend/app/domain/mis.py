"""What GlobalPact's MIS Tracker is told about a lead.

Pure mapping: no HTTP, no database. The MIS Tracker is the team's follow-up CRM, one
workspace per product (Life Herba, SustainX, Academicae). A lead that engages on
WhatsApp should appear there with an owner and a follow-up date, so a person picks up
what the campaign started.

Which leads go: only engaged ones. A campaign messages thousands of numbers; putting
every one of them in the MIS would bury the people who replied under the ones who never
read the message. `is_engaged` is that line, and the MIS side applies the same rule
before it creates a record (an existing record is still updated, so an opt-out reaches
a lead the team already holds).

PostgreSQL stays the source of truth and the sync is one-way, as with HubSpot: nothing
here reads back from the MIS. Status mapping lives on the MIS side (workspace config),
because the statuses are the team's and change with their process.
"""

from __future__ import annotations

from typing import Any

from app.domain.crm import LeadSnapshot, format_qualification
from app.domain.enums import LeadStatus

# Statuses that mean a person actually engaged. NEW / CONTACTED are campaign-side
# (messaged, no reply), FAILED never reached them.
ENGAGED_STATUSES = frozenset(
    {
        LeadStatus.RESPONDED,
        LeadStatus.INTERESTED,
        LeadStatus.QUALIFYING,
        LeadStatus.QUALIFIED,
        LeadStatus.DEMO_OFFERED,
        LeadStatus.DEMO_BOOKED,
        LeadStatus.NOT_INTERESTED,
        LeadStatus.OPTED_OUT,
        LeadStatus.INVALID,
    }
)


def is_engaged(status: LeadStatus | str) -> bool:
    return LeadStatus(status) in ENGAGED_STATUSES


def lead_link(dashboard_url: str | None, product_slug: str, lead_id: str) -> str | None:
    if not dashboard_url:
        return None
    return f"{dashboard_url.rstrip('/')}/p/{product_slug}/leads/{lead_id}"


def build_event(
    snapshot: LeadSnapshot,
    *,
    lead_id: str,
    product_slug: str,
    dashboard_url: str | None = None,
) -> dict[str, Any]:
    """The JSON body for MIS `POST /api/wa-event`.

    Blank values are omitted: the MIS only fills fields that are empty on its side, and
    an omitted key is clearer than an empty string about "the platform does not know".
    """
    data: dict[str, Any] = {
        "ref": lead_id,
        "phone": snapshot.phone_e164,
        "status": snapshot.status,
        "opted_out": snapshot.opted_out,
    }
    optional = {
        "person_name": snapshot.full_name,
        "organisation": snapshot.company,
        "designation": snapshot.designation,
        "email": snapshot.email,
        "city": snapshot.city,
        "audience": snapshot.source_audience_name,
        "campaign": snapshot.last_campaign_name,
        "segment": snapshot.segment_name,
        "summary": format_qualification(snapshot.qualification_data),
        "demo_at": snapshot.demo_start.isoformat() if snapshot.demo_start else None,
        "link": lead_link(dashboard_url, product_slug, lead_id),
    }
    for key, value in optional.items():
        if value not in (None, ""):
            data[key] = value
    return data


__all__ = ["ENGAGED_STATUSES", "build_event", "is_engaged", "lead_link"]
