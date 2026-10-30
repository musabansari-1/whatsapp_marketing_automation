"""Auditable lifecycle events for a campaign."""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.base import Base
from app.persistence.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class CampaignActivity(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "campaign_activities"

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (Index("ix_campaign_activities_campaign_created", "campaign_id", "created_at"),)
