"""Excel audiences and their contacts (specification §6, [D4], [D20])."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.enums import AudienceStatus, ContactValidationStatus
from app.persistence.base import Base
from app.persistence.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.persistence.models.campaign import Campaign


class Audience(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One uploaded Excel file, after validation.

    An audience is not usable until an admin confirms the preview (§6), which is
    what `status` records.
    """

    __tablename__ = "audiences"

    # §28 product scope. Nullable during the transition: `POST /audiences/upload` takes
    # no product and cannot derive one until Phase 19 supplies the internal-authoring
    # resolver. `tests/test_product_scope.py` records that debt and its closing phase.
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT")
    )
    # Nullable by design, permanently: §28 allows a product-wide audience, and a
    # product may have no segments at all.
    segment_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("segments.id", ondelete="RESTRICT")
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_filename: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[AudienceStatus] = mapped_column(
        Enum(AudienceStatus, name="audience_status", native_enum=True),
        nullable=False,
        default=AudienceStatus.PENDING_CONFIRMATION,
    )

    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    valid_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    invalid_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    contacts: Mapped[list[AudienceContact]] = relationship(
        back_populates="audience", cascade="all, delete-orphan"
    )
    campaigns: Mapped[list[Campaign]] = relationship(back_populates="audience")

    __table_args__ = (
        Index("ix_audiences_product_id", "product_id"),
        Index("ix_audiences_segment_id", "segment_id"),
        # A segment must belong to the audience's own product (§28.8), enforced as
        # referential integrity rather than a service check. MATCH SIMPLE skips the
        # check when either column is NULL, so a product-wide audience — and, during
        # the transition, an audience with no product yet — is unaffected.
        ForeignKeyConstraint(
            ["product_id", "segment_id"],
            ["segments.product_id", "segments.id"],
            name="fk_audiences_segment_within_product",
            ondelete="RESTRICT",
        ),
    )


class AudienceContact(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One spreadsheet row, valid or not.

    §6 requires storing the original row number, validation status, validation error
    and normalized values — invalid and duplicate rows included, so the admin can see
    what was rejected and why. That is why there is deliberately no unique constraint
    on the phone here: a duplicate row must still be storable, flagged as DUPLICATE.

    `raw_row` keeps the source data as uploaded. The production column schema comes
    from the client ([D4]/[D20]) and must not be invented, so parsed values beyond the
    normalized phone and email stay in JSONB until that schema is confirmed.
    """

    __tablename__ = "audience_contacts"

    audience_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("audiences.id", ondelete="CASCADE"), nullable=False
    )
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # Audience membership ([D55]). Set at confirmation for VALID rows, when the contact
    # is promoted to a lead.
    #
    # This is the relationship a campaign's recipient snapshot reads. `Lead
    # .source_audience_id` cannot serve that purpose: it records only where a lead was
    # *first* seen and is deliberately not updated on re-import, so a lead appearing in
    # a second audience stayed invisible to that audience's campaigns.
    #
    # Nullable because §6 stores INVALID and DUPLICATE rows too, and neither becomes a
    # lead. SET NULL rather than CASCADE on delete: the row is an audit record of what
    # was uploaded, and it must survive the lead going away.
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("leads.id", ondelete="SET NULL")
    )

    raw_row: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # The canonical V1 contact fields ([D25]). Nullable even for the required ones:
    # §6 stores invalid rows too, and an INVALID row is precisely one where a required
    # value was missing or unusable.
    full_name: Mapped[str | None] = mapped_column(String(255))
    phone_e164: Mapped[str | None] = mapped_column(String(20))
    email: Mapped[str | None] = mapped_column(String(320))
    company: Mapped[str | None] = mapped_column(String(255))
    designation: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(255))

    validation_status: Mapped[ContactValidationStatus] = mapped_column(
        Enum(ContactValidationStatus, name="contact_validation_status", native_enum=True),
        nullable=False,
    )
    validation_error: Mapped[str | None] = mapped_column(Text)

    audience: Mapped[Audience] = relationship(back_populates="contacts")

    __table_args__ = (
        Index("ix_audience_contacts_audience_id", "audience_id"),
        Index("ix_audience_contacts_audience_status", "audience_id", "validation_status"),
        Index("ix_audience_contacts_phone_e164", "phone_e164"),
        # The recipient snapshot joins leads to this table by audience.
        Index("ix_audience_contacts_lead_id", "lead_id"),
    )
