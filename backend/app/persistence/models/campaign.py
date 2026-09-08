"""Campaigns, recipient snapshots and WhatsApp templates (§7, §8, [D6])."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.enums import CampaignStatus, RecipientStatus, TemplateCategory
from app.persistence.base import Base
from app.persistence.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.persistence.models.audience import Audience
    from app.persistence.models.lead import Lead
    from app.persistence.models.user import User

# Meta's approval value for a usable template. Stored verbatim rather than mapped to
# a local enum: [D6] forbids inventing a local approval status, and Meta may add
# values we must not silently reject.
META_STATUS_APPROVED = "APPROVED"


class WhatsAppTemplate(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Approved template metadata synchronized from Meta ([D6]).

    `provider_status` holds Meta's own value as a string. The application never
    assigns approval itself; it records what Meta reported and filters on it.
    """

    __tablename__ = "whatsapp_templates"

    # §28 product scope. Nullable during the transition: template sync pulls a
    # WABA's templates with no product context until Phase 23 makes the sync
    # product-aware. The global (name, language) uniqueness therefore stays in
    # force until then — see the Phase 18 note in PROJECT_STATUS.md.
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT")
    )

    name: Mapped[str] = mapped_column(String(512), nullable=False)
    language: Mapped[str] = mapped_column(String(20), nullable=False)
    provider_template_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    provider_status: Mapped[str] = mapped_column(String(50), nullable=False)
    category: Mapped[TemplateCategory] = mapped_column(
        Enum(TemplateCategory, name="template_category", native_enum=True), nullable=False
    )
    variable_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    body_preview: Mapped[str | None] = mapped_column(Text)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    campaigns: Mapped[list[Campaign]] = relationship(back_populates="template")

    __table_args__ = (
        # Meta identifies a template by name plus language.
        UniqueConstraint("name", "language", name="uq_whatsapp_templates_name_language"),
        Index("ix_whatsapp_templates_provider_status", "provider_status"),
        Index("ix_whatsapp_templates_product_id", "product_id"),
    )


class Campaign(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A broadcast. Human-initiated (§7); sending itself is asynchronous."""

    __tablename__ = "campaigns"

    # §28 product scope. Nullable during the transition: a campaign inherits its
    # product from its audience, which has none until Phase 19.
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT")
    )
    # Nullable by design, permanently: §28 allows a product-wide entity, and a
    # product may have no segments at all. Never filled with a placeholder.
    segment_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("segments.id", ondelete="RESTRICT")
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[CampaignStatus] = mapped_column(
        Enum(CampaignStatus, name="campaign_status", native_enum=True),
        nullable=False,
        default=CampaignStatus.DRAFT,
    )

    audience_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("audiences.id", ondelete="RESTRICT"), nullable=False
    )
    template_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("whatsapp_templates.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Denormalized counters for the §17 overview. Derived, never authoritative.
    total_recipients: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delivered_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    read_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    audience: Mapped[Audience] = relationship(back_populates="campaigns")
    template: Mapped[WhatsAppTemplate] = relationship(back_populates="campaigns")
    created_by: Mapped[User | None] = relationship(back_populates="created_campaigns")
    recipients: Mapped[list[CampaignRecipient]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_campaigns_status", "status"),
        Index("ix_campaigns_audience_id", "audience_id"),
        Index("ix_campaigns_template_id", "template_id"),
        Index("ix_campaigns_product_id", "product_id"),
        Index("ix_campaigns_segment_id", "segment_id"),
        # A segment must belong to this row's own product (§28.8). MATCH SIMPLE
        # skips the check when either column is NULL, so a product-wide row is
        # unaffected, and so is one with no product yet during the transition.
        ForeignKeyConstraint(
            ["product_id", "segment_id"],
            ["segments.product_id", "segments.id"],
            name="fk_campaigns_segment_within_product",
            ondelete="RESTRICT",
        ),
    )


class CampaignRecipient(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """The recipient snapshot (§7).

    Rows are frozen when the campaign is prepared, so later audience edits cannot
    change a campaign already under way.

    The unique constraint on (campaign_id, lead_id) is the idempotency guarantee §20
    requires for campaign recipients: a retried preparation cannot enqueue a second
    send to the same lead.
    """

    __tablename__ = "campaign_recipients"

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )

    # Snapshot of the number at preparation time.
    phone_e164: Mapped[str] = mapped_column(String(20), nullable=False)

    status: Mapped[RecipientStatus] = mapped_column(
        Enum(RecipientStatus, name="recipient_status", native_enum=True),
        nullable=False,
        default=RecipientStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)

    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    campaign: Mapped[Campaign] = relationship(back_populates="recipients")
    lead: Mapped[Lead] = relationship()

    __table_args__ = (
        UniqueConstraint("campaign_id", "lead_id", name="uq_campaign_recipients_campaign_lead"),
        Index("ix_campaign_recipients_campaign_status", "campaign_id", "status"),
        Index("ix_campaign_recipients_lead_id", "lead_id"),
    )
