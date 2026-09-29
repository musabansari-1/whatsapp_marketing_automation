"""Canonical state enumerations.

These are domain concepts, not persistence or transport details, so they live here
and are imported by both. Values are transcribed verbatim from SPECIFICATION.md and
must not be extended without a specification change — §10 is explicit that the AI
cannot invent state transitions, and the same discipline applies to the states
themselves.

Where an enum is not enumerated in the specification, the source is noted on the
class so review can see what was derived rather than transcribed.
"""

from __future__ import annotations

from enum import StrEnum


class UserRole(StrEnum):
    """[D2]. Admin manages internal users; there is no public registration."""

    ADMIN = "ADMIN"
    SALESPERSON = "SALESPERSON"


class CampaignStatus(StrEnum):
    """Specification §7."""

    DRAFT = "DRAFT"
    READY = "READY"
    SCHEDULED = "SCHEDULED"
    PROCESSING = "PROCESSING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class LeadStatus(StrEnum):
    """Specification §10."""

    NEW = "NEW"
    CONTACTED = "CONTACTED"
    RESPONDED = "RESPONDED"
    INTERESTED = "INTERESTED"
    QUALIFYING = "QUALIFYING"
    QUALIFIED = "QUALIFIED"
    DEMO_OFFERED = "DEMO_OFFERED"
    DEMO_BOOKED = "DEMO_BOOKED"
    NOT_INTERESTED = "NOT_INTERESTED"
    OPTED_OUT = "OPTED_OUT"
    INVALID = "INVALID"
    FAILED = "FAILED"


class ConversationStatus(StrEnum):
    """Specification §10."""

    ACTIVE = "ACTIVE"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    BOOKING = "BOOKING"
    CLOSED = "CLOSED"


class MessageStatus(StrEnum):
    """Specification §8."""

    QUEUED = "QUEUED"
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    READ = "READ"
    FAILED = "FAILED"


class MessageDirection(StrEnum):
    """Derived. §8 tracks inbound and outbound messages in one `messages` table,
    so the direction must be recorded to tell them apart."""

    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class BookingStatus(StrEnum):
    """Specification §15."""

    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class TemplateCategory(StrEnum):
    """Meta's template categories. Marketing broadcasts require MARKETING ([D6])."""

    MARKETING = "MARKETING"
    UTILITY = "UTILITY"
    AUTHENTICATION = "AUTHENTICATION"


class TemplateHeaderType(StrEnum):
    """The header component a Meta template carries, as Meta reported it.

    Modeled because a send must mirror the template's shape, not a shape we prefer.
    Meta rejects a send whose components do not match the approved template (132012,
    "header: Format mismatch"), so a template with an IMAGE header cannot be sent by
    omitting the header the way a body-only send does.

    `NONE` is a template with no header at all, which is the only value that needs no
    media. The media types are named after Meta's own `HEADER` component types rather
    than a locally invented vocabulary, for the same reason [D6] compares Meta's
    approval string as-is.
    """

    NONE = "NONE"
    TEXT = "TEXT"
    IMAGE = "IMAGE"
    VIDEO = "VIDEO"
    DOCUMENT = "DOCUMENT"

    @property
    def needs_media(self) -> bool:
        """Whether sending this template requires a media URL from the application.

        TEXT headers carry their own text, and a template with no header needs nothing.
        IMAGE, VIDEO and DOCUMENT are satisfied only by a link, which the approved
        template's own sample does not provide: Meta's stored example is a placeholder
        shown in the manager, not the media the message is meant to carry.
        """
        return self in (
            TemplateHeaderType.IMAGE,
            TemplateHeaderType.VIDEO,
            TemplateHeaderType.DOCUMENT,
        )


class ContactValidationStatus(StrEnum):
    """Derived from the §6 pipeline, which requires storing invalid and duplicate
    rows alongside valid ones together with the reason."""

    VALID = "VALID"
    INVALID = "INVALID"
    DUPLICATE = "DUPLICATE"


class AudienceStatus(StrEnum):
    """Derived from §6: an audience is previewed before the admin confirms it, so
    the two states must be distinguishable."""

    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    CONFIRMED = "CONFIRMED"


class RecipientStatus(StrEnum):
    """Derived. §7 gives recipients a status without enumerating it.

    PENDING/QUEUED/SENT/DELIVERED/READ/FAILED mirror the §8 message lifecycle.
    SKIPPED is required by [D16]: a recipient suppressed by the global opt-out
    check is never sent to and needs a terminal state that is not FAILED.
    """

    PENDING = "PENDING"
    QUEUED = "QUEUED"
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    READ = "READ"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class DocumentStatus(StrEnum):
    """Derived from §11, which names READY explicitly and requires surfacing
    ingestion errors and an archive action (§16, §17)."""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    READY = "READY"
    FAILED = "FAILED"
    ARCHIVED = "ARCHIVED"


class IngestionJobStatus(StrEnum):
    """Derived from the §11 ingestion pipeline."""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class WebhookEventStatus(StrEnum):
    """Derived from §8: webhooks persist quickly, then work is processed
    asynchronously, so the record needs to distinguish the two."""

    RECEIVED = "RECEIVED"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"


class AIRunStatus(StrEnum):
    """Derived from §21, which requires AI failures to be observable."""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class NotificationType(StrEnum):
    """§13 gives notifications a `type`. Bookings notify; the bot-activity audit
    flags ([D67]). Each addition is a scope decision, because a type is a promise
    about what the bell can say."""

    DEMO_BOOKED = "DEMO_BOOKED"
    #: A conversation whose inbound pattern looks automated (pace, regularity,
    #: marathon length). Detection only: flagging never gates a reply, so a wrong
    #: guess costs a dismissed notification rather than a lost customer.
    BOT_SUSPECTED = "BOT_SUSPECTED"


class SuppressionReason(StrEnum):
    """Derived from [D5]/[D16]. Opt-out is the V1 path; the others record why a
    number is suppressed when it was not a customer keyword."""

    OPT_OUT_KEYWORD = "OPT_OUT_KEYWORD"
    MANUAL = "MANUAL"
    PROVIDER_INVALID = "PROVIDER_INVALID"
