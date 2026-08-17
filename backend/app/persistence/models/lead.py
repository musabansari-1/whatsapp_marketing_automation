"""Leads and the global WhatsApp suppression list (§10, §15, [D5], [D11], [D16], [D18])."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, String, Text
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

    phone_e164: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    email: Mapped[str | None] = mapped_column(String(320))
    full_name: Mapped[str | None] = mapped_column(String(255))

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

    conversations: Mapped[list[Conversation]] = relationship(back_populates="lead")
    bookings: Mapped[list[Booking]] = relationship(back_populates="lead")

    __table_args__ = (
        Index("ix_leads_status", "status"),
        Index("ix_leads_email", "email"),
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
