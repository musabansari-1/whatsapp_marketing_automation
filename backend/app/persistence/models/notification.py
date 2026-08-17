"""Dashboard notifications (§13, [D3])."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.enums import NotificationType
from app.persistence.base import Base
from app.persistence.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.persistence.models.user import User


class Notification(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A dashboard notification for one internal user.

    Fields follow §13. A booking produces one row per active internal user ([D3]),
    since V1 has no lead assignment.

    The unique constraint on (user_id, type, related_entity_type, related_entity_id)
    is what makes that fan-out idempotent, as §20 requires. Notification delivery may
    be retried after a partial failure — §12 is explicit that a confirmed booking
    stays confirmed while a failed notification retries — and without this constraint
    every retry would add duplicate rows to the users who already succeeded.
    """

    __tablename__ = "notifications"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[NotificationType] = mapped_column(
        Enum(NotificationType, name="notification_type", native_enum=True), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)

    related_entity_type: Mapped[str | None] = mapped_column(String(50))
    related_entity_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))

    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="notifications")

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "type",
            "related_entity_type",
            "related_entity_id",
            name="uq_notifications_user_type_entity",
        ),
        # The bell badge queries unread notifications for the current user.
        Index("ix_notifications_user_unread", "user_id", "is_read"),
        Index("ix_notifications_created_at", "created_at"),
    )
