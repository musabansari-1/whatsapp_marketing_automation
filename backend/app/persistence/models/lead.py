"""Leads and the global WhatsApp suppression list (§10, §15, [D5], [D11], [D16], [D18])."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.enums import LeadStatus, SuppressionReason
from app.persistence.base import Base
from app.persistence.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.persistence.models.booking import Booking
    from app.persistence.models.conversation import Conversation


class Lead(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A customer reachable on WhatsApp.

    `phone_e164` is unique: [D11] names the normalized WhatsApp phone number as the
    primary deduplication identity, so re-uploading the same number must resolve to
    one lead rather than creating a second.

    `qualification_data` holds AI-extracted facts ([D18]). The backend alone decides
    `status` — the model writes here, never there ([D9]).
    """

    __tablename__ = "leads"

    # §28 product scope. Nullable during the transition: leads are created by
    # audience promotion and by inbound webhooks, and neither can supply a
    # product until Phase 19.
    #
    # There is deliberately **no** `segment_id` here ([D48]). A lead may belong
    # to several segments within a product, so a single column would be
    # misleading, duplicate membership state, and eventually be mistaken for
    # authority. Segment membership is `audience_contacts` -> `audiences
    # .segment_id`; the operative segment for AI behaviour lives on the
    # conversation snapshot.
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT")
    )

    phone_e164: Mapped[str] = mapped_column(String(20), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320))
    # Carried through from the audience so promotion discards nothing ([D25]).
    company: Mapped[str | None] = mapped_column(String(255))
    designation: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(255))

    status: Mapped[LeadStatus] = mapped_column(
        Enum(LeadStatus, name="lead_status", native_enum=True),
        nullable=False,
        default=LeadStatus.NEW,
    )

    # AI-extracted structured information. Never the deciding authority on `status`.
    qualification_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    # Convenience mirror of the suppression table for display and joins. The
    # authoritative check before sending is whatsapp_suppressions ([D16]); this
    # column alone is never sufficient.
    opted_out: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Provenance: which audience introduced this lead.
    source_audience_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("audiences.id", ondelete="SET NULL")
    )

    last_contacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # CRM synchronization state (§14). Recorded so a retry can update the contact it
    # already created rather than searching HubSpot again, and so the dashboard can
    # show that a lead has not reached the CRM.
    #
    # Not a unique constraint: HubSpot can legitimately hold one contact matched by
    # email for two platform leads with different phone numbers, and refusing to
    # record that would lose the link entirely.
    crm_contact_id: Mapped[str | None] = mapped_column(String(64))
    crm_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The last failure, kept until a sync succeeds. §14 forbids a CRM outage breaking
    # anything, so failures are recorded rather than raised at the caller.
    crm_sync_error: Mapped[str | None] = mapped_column(Text)

    conversations: Mapped[list[Conversation]] = relationship(back_populates="lead")
    bookings: Mapped[list[Booking]] = relationship(back_populates="lead")

    __table_args__ = (
        Index("ix_leads_status", "status"),
        Index("ix_leads_email", "email"),
        Index("ix_leads_product_id", "product_id"),
        # Lead identity is (product, phone) ([D41]). Two partial indexes rather than
        # one plain unique: `product_id` stays nullable until Phase 23, and SQL treats
        # NULLs as distinct, so a plain UNIQUE (product_id, phone_e164) would enforce
        # nothing at all for the transitional rows that most need it.
        Index(
            "uq_leads_product_phone",
            "product_id",
            "phone_e164",
            unique=True,
            postgresql_where=text("product_id IS NOT NULL"),
        ),
        # Transitional: one lead per phone in the not-yet-routed lane. Removed in
        # Phase 23 together with the nullability it covers.
        Index(
            "uq_leads_transitional_phone",
            "phone_e164",
            unique=True,
            postgresql_where=text("product_id IS NULL"),
        ),
        Index("ix_leads_source_audience_id", "source_audience_id"),
        # The [D18] qualification rules read inside this document.
        Index("ix_leads_qualification_data", "qualification_data", postgresql_using="gin"),
    )


class WhatsAppSuppression(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Global, phone-level opt-out ([D5], [D16]).

    Deliberately not a column on `leads`: the suppression must survive independently
    of any lead row, so that deleting, re-importing or re-creating a lead cannot
    resurrect consent. Campaign sending checks this table before every send.

    `lead_id` is a nullable back-reference for display only — a number can be
    suppressed before any lead exists for it.
    """

    __tablename__ = "whatsapp_suppressions"

    phone_e164: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    reason: Mapped[SuppressionReason] = mapped_column(
        Enum(SuppressionReason, name="suppression_reason", native_enum=True),
        nullable=False,
        default=SuppressionReason.OPT_OUT_KEYWORD,
    )
    # The customer message that triggered the opt-out, for audit.
    source_message_id: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)

    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("leads.id", ondelete="SET NULL")
    )

    __table_args__ = (Index("ix_whatsapp_suppressions_lead_id", "lead_id"),)
