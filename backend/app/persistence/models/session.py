"""Server-side authentication sessions ([D2]).

NOTE — this table is not in the specification §15 list. It is an implementation
mechanism for [D2] rather than a new product requirement, and it is reported as a
deviation rather than introduced silently.

Why a table rather than a self-contained signed cookie:

[D2] requires session-cookie authentication and rejects JWT. A signed cookie carrying
its own claims is JWT in all but encoding — and, more importantly, cannot be revoked.
`POST /auth/logout` would only clear the browser's copy while a stolen cookie stayed
valid until expiry, and deactivating a user (which [D3] makes a real operation, since
notifications fan out to *active* users) would not end their session.

Server-side sessions make revocation real: logout and deactivation delete or expire
the row, and the next request fails.

Only the SHA-256 digest of the token is stored, so a database leak does not yield
usable sessions.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.persistence.base import Base
from app.persistence.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.persistence.models.user import User


class Session(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One authenticated browser session."""

    __tablename__ = "sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # SHA-256 hex digest of the opaque token. Unique so lookup is a single indexed
    # read and two sessions cannot collide.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="sessions")

    __table_args__ = (
        Index("ix_sessions_user_id", "user_id"),
        # Supports sweeping expired rows.
        Index("ix_sessions_expires_at", "expires_at"),
    )
