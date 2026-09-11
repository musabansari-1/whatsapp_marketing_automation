"""Demo bookings (§12, §15, [D7], [D8])."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.enums import BookingStatus
from app.persistence.base import Base
from app.persistence.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.persistence.models.lead import Lead


class Booking(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A demo booking. Fields follow §15.

    `calendar_event_id` is unique, which is what enforces §12's prohibition on a
    retried booking task producing a second calendar event. Combined with the
    client-generated event IDs the calendar provider accepts, a retry converges on
    the existing row instead of creating another.

    It is nullable because a booking may be recorded as PENDING or FAILED before
    Google confirms — and §12 is explicit that no booking is confirmed until the
    provider says so.

    The partial unique index on `(product_id, start_time)` for confirmed bookings is the
    database's share of the race handling §12 requires: the pre-booking recheck narrows
    the window, and this closes it, since two concurrent confirmations for the same slot
    in one product cannot both commit. Scoped to the product because each product books
    into its own calendar ([D43]) — Academicae, SustainX and Life Herba may all confirm
    Tuesday 10:00 at once.
    """

    __tablename__ = "bookings"

    # §28 product scope, NOT NULL since Phase 24. Taken from the conversation the booking
    # was made in (§28.5) and never chosen by the booking tool, so a booking cannot land
    # in a product other than the one whose calendar confirmed it.
    product_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )

    lead_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL")
    )

    calendar_event_id: Mapped[str | None] = mapped_column(String(255), unique=True)

    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # IANA zone name, e.g. "Asia/Kolkata". Stored per booking so a policy change
    # cannot retroactively reinterpret an existing booking ([D8]).
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[BookingStatus] = mapped_column(
        Enum(BookingStatus, name="booking_status", native_enum=True),
        nullable=False,
        default=BookingStatus.PENDING,
    )

    meeting_link: Mapped[str | None] = mapped_column(String(1024))
    customer_email: Mapped[str | None] = mapped_column(String(320))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lead: Mapped[Lead] = relationship(back_populates="bookings")

    __table_args__ = (
        # The naming convention prefixes "ck_<table>_", so the name here is the
        # suffix only.
        CheckConstraint("end_time > start_time", name="end_after_start"),
        Index("ix_bookings_lead_id", "lead_id"),
        Index("ix_bookings_product_id", "product_id"),
        Index("ix_bookings_conversation_id", "conversation_id"),
        Index("ix_bookings_status", "status"),
        Index("ix_bookings_start_time", "start_time"),
        # Partial: only a CONFIRMED booking occupies a slot. Cancelled and failed
        # attempts must not block rebooking the same time. Per product, because each
        # product's calendar is its own ([D43]).
        Index(
            "uq_bookings_product_confirmed_start_time",
            "product_id",
            "start_time",
            unique=True,
            postgresql_where=text("status = 'CONFIRMED'"),
        ),
    )
