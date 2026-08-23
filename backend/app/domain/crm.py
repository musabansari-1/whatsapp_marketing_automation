"""What the CRM is told about a lead (specification §14, [D11], [D18]).

Pure mapping: no HTTP, no database. It decides which of the platform's facts become
CRM properties, which is a product judgement rather than a transport detail and belongs
somewhere reviewable.

§14 names six things to synchronize — contact information, lead state, qualification
information, source/campaign information, demo booking information, and opt-out state.
This maps each one.

Two rules shape it:

- **PostgreSQL is the source of truth.** Nothing here reads *back* from the CRM to
  change platform state. Synchronization is one-way, so a stale or edited CRM record
  can never overwrite what the platform knows.
- **Only what the sales team needs.** The full transcript is not synchronized: it can
  be long, it is personal, and pushing it into a third-party system nobody asked to
  hold it is a decision nobody made.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Property names as they appear in HubSpot. `firstname`, `lastname`, `email`, `phone`,
# `company` and `jobtitle` are HubSpot's own contact defaults; the rest are custom
# properties this application expects to exist there.
#
# Named here rather than inline so the set the client must create in HubSpot can be
# read off one list — see the phase notes.
PROP_FIRST_NAME = "firstname"
PROP_LAST_NAME = "lastname"
PROP_EMAIL = "email"
PROP_PHONE = "phone"
PROP_COMPANY = "company"
PROP_JOB_TITLE = "jobtitle"
PROP_CITY = "city"

PROP_LEAD_STATUS = "wa_lead_status"
PROP_LEAD_SOURCE = "wa_lead_source"
PROP_CAMPAIGN = "wa_campaign"
PROP_OPTED_OUT = "wa_opted_out"
PROP_QUALIFICATION = "wa_qualification"
PROP_DEMO_AT = "wa_demo_at"
PROP_DEMO_STATUS = "wa_demo_status"
PROP_LAST_SYNCED = "wa_last_synced_at"

# The custom properties the client must create in HubSpot before synchronization does
# anything useful. HubSpot silently ignores unknown properties on write, so a missing
# one loses data without erroring — which is why this list exists.
CUSTOM_PROPERTIES = (
    PROP_LEAD_STATUS,
    PROP_LEAD_SOURCE,
    PROP_CAMPAIGN,
    PROP_OPTED_OUT,
    PROP_QUALIFICATION,
    PROP_DEMO_AT,
    PROP_DEMO_STATUS,
    PROP_LAST_SYNCED,
)

# A cap on the serialized qualification blob. The AI can extract a lot, HubSpot's
# single-line text properties are limited, and truncating here is better than the CRM
# silently rejecting the write.
MAX_QUALIFICATION_CHARS = 900


@dataclass(frozen=True, slots=True)
class LeadSnapshot:
    """What the sync knows about a lead at the moment it runs.

    A plain value rather than the ORM row, so the mapping stays testable and the domain
    layer does not depend on persistence.
    """

    phone_e164: str
    full_name: str | None = None
    email: str | None = None
    company: str | None = None
    designation: str | None = None
    city: str | None = None
    status: str = ""
    qualification_data: dict[str, Any] | None = None
    opted_out: bool = False
    source_audience_name: str | None = None
    last_campaign_name: str | None = None
    demo_start: datetime | None = None
    demo_status: str | None = None
    demo_timezone: str | None = None


def split_name(full_name: str | None) -> tuple[str | None, str | None]:
    """Split a display name into HubSpot's first and last name fields.

    HubSpot has no single "name" property, so a name has to be split somewhere. The
    last whitespace-separated token becomes the surname and everything before it the
    forename — wrong for some naming conventions, but the alternative is putting the
    whole name in `firstname`, which sorts and greets badly.

    A single-token name stays entirely in `firstname`: guessing that "Asha" is a
    surname would be worse than leaving it unset.
    """
    if not full_name or not full_name.strip():
        return None, None

    parts = full_name.strip().split()
    if len(parts) == 1:
        return parts[0], None
    return " ".join(parts[:-1]), parts[-1]


def format_qualification(data: dict[str, Any] | None) -> str | None:
    """Render the AI's extracted facts as readable text.

    Not JSON: a salesperson reads this inside HubSpot, and `{"team_size": 40}` is
    worse than `team_size: 40` for that audience. [D18] means these are the model's
    extraction, so they are informational in the CRM and never authoritative.
    """
    if not data:
        return None

    lines = [f"{key}: {value}" for key, value in sorted(data.items()) if value not in (None, "")]
    if not lines:
        return None

    text = "\n".join(lines)
    if len(text) > MAX_QUALIFICATION_CHARS:
        return text[: MAX_QUALIFICATION_CHARS - 1] + "…"
    return text


def build_properties(snapshot: LeadSnapshot, *, synced_at: datetime) -> dict[str, Any]:
    """The CRM properties for one lead.

    Empty values are omitted rather than sent as blanks. HubSpot treats an empty string
    as a deliberate clear, so sending one for a field the platform simply does not know
    would erase whatever a salesperson had typed there by hand.
    """
    first_name, last_name = split_name(snapshot.full_name)

    properties: dict[str, Any] = {
        # Phone is the identity ([D11]) and is always present.
        PROP_PHONE: snapshot.phone_e164,
        PROP_LEAD_STATUS: snapshot.status,
        # Always sent, including False: consent is exactly the field where a stale
        # "yes" is dangerous, so it is never left to be inferred from absence.
        PROP_OPTED_OUT: "true" if snapshot.opted_out else "false",
        PROP_LAST_SYNCED: _iso(synced_at),
    }

    optional = {
        PROP_FIRST_NAME: first_name,
        PROP_LAST_NAME: last_name,
        PROP_EMAIL: snapshot.email,
        PROP_COMPANY: snapshot.company,
        PROP_JOB_TITLE: snapshot.designation,
        PROP_CITY: snapshot.city,
        # An uploaded lead names its audience; one that messaged first has no
        # audience, and saying so is more useful than leaving the field blank.
        PROP_LEAD_SOURCE: snapshot.source_audience_name or "WhatsApp inbound",
        PROP_CAMPAIGN: snapshot.last_campaign_name,
        PROP_QUALIFICATION: format_qualification(snapshot.qualification_data),
        PROP_DEMO_AT: _iso(snapshot.demo_start) if snapshot.demo_start else None,
        PROP_DEMO_STATUS: snapshot.demo_status,
    }

    for key, value in optional.items():
        if value not in (None, ""):
            properties[key] = value

    return properties


def _iso(moment: datetime) -> str:
    return moment.isoformat()


__all__ = [
    "CUSTOM_PROPERTIES",
    "MAX_QUALIFICATION_CHARS",
    "PROP_CAMPAIGN",
    "PROP_CITY",
    "PROP_COMPANY",
    "PROP_DEMO_AT",
    "PROP_DEMO_STATUS",
    "PROP_EMAIL",
    "PROP_FIRST_NAME",
    "PROP_JOB_TITLE",
    "PROP_LAST_NAME",
    "PROP_LAST_SYNCED",
    "PROP_LEAD_SOURCE",
    "PROP_LEAD_STATUS",
    "PROP_OPTED_OUT",
    "PROP_PHONE",
    "PROP_QUALIFICATION",
    "LeadSnapshot",
    "build_properties",
    "format_qualification",
    "split_name",
]
