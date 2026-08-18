"""Internal users (specification §15, [D2], [D3])."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.enums import UserRole
from app.persistence.base import Base
from app.persistence.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.persistence.models.campaign import Campaign
    from app.persistence.models.notification import Notification
    from app.persistence.models.session import Session


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """An internal dashboard user. There is no public registration ([D2]).

    `is_active` is load-bearing rather than cosmetic: [D3] fans a booking
    notification out to every *active* internal user, and an inactive user must not
    be able to authenticate.
    """

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    # Only ever a hash. §19 forbids storing credentials in recoverable form.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role", native_enum=True), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    notifications: Mapped[list[Notification]] = relationship(back_populates="user")
    # Cascade so deleting a user cannot leave live sessions behind.
    sessions: Mapped[list[Session]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    created_campaigns: Mapped[list[Campaign]] = relationship(back_populates="created_by")

    __table_args__ = (
        # The [D3] fan-out queries active users on every booking.
        Index("ix_users_is_active", "is_active"),
    )
