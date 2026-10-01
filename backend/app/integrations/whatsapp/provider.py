"""WhatsApp provider interface (specification §8).

    WhatsAppProvider
     ├── MetaWhatsAppProvider   (Phase 6)
     └── FakeWhatsAppProvider

Template approval is Meta's, never ours ([D6]): `fetch_approved_templates` reads
what Meta has approved, and no code path may invent an approval status.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

# Canonical definitions live in the domain layer so persistence and integrations
# agree on one set of states. Re-exported here for callers of this package.
from app.domain.enums import MessageStatus, TemplateCategory, TemplateHeaderType

__all__ = [
    "ApprovedTemplate",
    "InboundMessage",
    "MessageStatus",
    "OutboundTemplateMessage",
    "OutboundTextMessage",
    "SentMessage",
    "TemplateCategory",
    "TemplateHeaderType",
    "WhatsAppProvider",
]


@dataclass(frozen=True, slots=True)
class SentMessage:
    """Result of a send. `provider_message_id` is Meta's ID, tracked per §8."""

    provider_message_id: str
    status: MessageStatus


@dataclass(frozen=True, slots=True)
class ApprovedTemplate:
    """Template metadata synchronized from Meta ([D6]).

    `provider_status` carries Meta's own value through unchanged. The synchronization
    stores every template it is given, status included, and callers filter on it —
    nothing in this application decides approval.
    """

    name: str
    language: str
    category: TemplateCategory
    provider_template_id: str
    variable_count: int = 0
    body_preview: str | None = None
    provider_status: str = ""
    # What the approved template's header looks like, as Meta reported it. Carried for
    # the same reason approval is: the send has to mirror the template, so a header
    # that is not known here cannot be reproduced ([D6] applies to the whole shape).
    header_type: TemplateHeaderType = TemplateHeaderType.NONE
    # The media link Meta recorded for a media header. This is the template's *sample*,
    # kept for reference only — it is never sent, because the approved example is not
    # necessarily the asset the campaign is meant to deliver.
    header_sample_url: str | None = None


@dataclass(frozen=True, slots=True)
class OutboundTemplateMessage:
    """A template send request.

    `idempotency_key` lets the provider and the worker collapse duplicate sends,
    which §20 requires for sending tasks.

    `header_media_url` is the asset a media header carries. It is supplied by the
    caller rather than read from the template, because the URL stored alongside an
    approved template is Meta's *sample* and not necessarily what this campaign is
    meant to send. It must be set whenever the template's header needs media; a
    template whose header is absent or plain text leaves it None.
    """

    to_e164: str
    template_name: str
    language: str
    variables: tuple[str, ...] = ()
    idempotency_key: str | None = None
    header_type: TemplateHeaderType = TemplateHeaderType.NONE
    header_media_url: str | None = None


@dataclass(frozen=True, slots=True)
class OutboundTextMessage:
    """A free-form text send request.

    Free-form messages are only deliverable inside Meta's 24-hour customer service
    window. Window enforcement is Phase 6 business logic, not a provider concern.
    """

    to_e164: str
    body: str
    idempotency_key: str | None = None


@dataclass(slots=True)
class InboundMessage:
    """A customer message received via webhook (Phase 6/9 consume this)."""

    provider_message_id: str
    from_e164: str
    body: str
    received_at: datetime
    metadata: dict[str, str] = field(default_factory=dict)


class WhatsAppProvider(ABC):
    """Outbound WhatsApp operations.

    Webhook parsing is deliberately absent: inbound handling is an API concern
    (Phase 6), not a provider capability.
    """

    name: str

    @abstractmethod
    async def send_template_message(self, message: OutboundTemplateMessage) -> SentMessage:
        """Send an approved template message."""

    @abstractmethod
    async def send_text_message(self, message: OutboundTextMessage) -> SentMessage:
        """Send a free-form text message."""

    @abstractmethod
    async def fetch_approved_templates(self) -> list[ApprovedTemplate]:
        """Return templates Meta has approved, for synchronization into the database."""

    @abstractmethod
    async def mark_read_with_typing(self, provider_message_id: str) -> None:
        """Mark an inbound message read and show a typing indicator.

        Meta dismisses the indicator when the reply lands or after 25 seconds,
        so it can never stick on screen. A status update, not a send: no
        idempotency key, no result, and callers treat failure as best-effort —
        losing a turn over a read receipt would be absurd.
        """
