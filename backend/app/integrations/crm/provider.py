"""CRM provider interface (specification §14, [D11]).

    CRMProvider
     ├── HubSpotProvider  (Phase 12)
     └── FakeCRMProvider

HubSpot is an integration, not the operational database: PostgreSQL stays the source
of truth and a CRM outage must never break WhatsApp, AI, RAG, Calendar, the dashboard
or core state.

Identity ([D11]): the normalized E.164 phone number is primary and email secondary.
`find_contact` takes both so a provider can try phone first and fall back to email —
which matters because HubSpot's own contact uniqueness is email-based.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class CRMContact:
    """A contact as the CRM sees it."""

    provider_contact_id: str
    phone_e164: str | None = None
    email: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ContactUpsert:
    """A synchronization request.

    `idempotency_key` lets a retried sync task converge on one contact (§20).
    """

    phone_e164: str
    email: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None


class CRMProvider(ABC):
    name: str

    @abstractmethod
    async def find_contact(
        self, *, phone_e164: str | None = None, email: str | None = None
    ) -> CRMContact | None:
        """Look up a contact by phone (primary) or email (secondary), or None."""

    @abstractmethod
    async def upsert_contact(self, upsert: ContactUpsert) -> CRMContact:
        """Create or update the contact identified by phone, falling back to email."""
