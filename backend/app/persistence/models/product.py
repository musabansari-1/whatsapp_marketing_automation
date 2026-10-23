"""Products, segments and AI configuration (§28, [D41], [D48], [D49], [D52]).

The three tables the multi-product architecture rests on. Nothing consumes them yet:
Phase 17 establishes the shape, and each later phase adds the columns it actually
needs to the product as it comes to use them. A table carrying configuration nothing
reads is configuration that drifts.

**No product is a schema concept.** There is no column, enum or constraint here that
knows about Product 1, SustainX or Life Herba. Adding a fourth product is an insert.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.persistence.base import Base
from app.persistence.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    pass


class Product(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One marketed product (§28.2).

    Deliberately minimal. The WhatsApp identity, calendar, audience column schema and
    slot policy overrides described in §28 arrive with the phases that read them —
    Phase 23, Phase 24 and Phase 27 — rather than sitting here unused and unvalidated.

    `slug` is the stable external identifier. Phase 26 puts it in `/p/{slug}/…` routes,
    at which point changing it breaks every bookmarked and shared link, so it is a
    commitment in a way `display_name` is not. `display_name` may change freely; nothing
    references it.
    """

    __tablename__ = "products"

    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # An inactive product keeps its data and stops being offered for new work. Whether
    # anything enforces that is Phase 26's business; the column exists so deactivating
    # never means deleting.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # ---------------------------------------------------------------- WhatsApp (§28)
    #
    # One Meta App and **one shared WABA**, with a phone number per product. So the
    # number — not the WABA — is what identifies a product, and it is the inbound
    # routing key ([D40]).
    #
    # Nullable because a product need not use WhatsApp, and because the real Meta IDs
    # do not exist yet. Production validation belongs to Phase 27.
    whatsapp_phone_number_id: Mapped[str | None] = mapped_column(String(64))

    # Shared today, so deliberately **not** unique. Kept per product anyway: a later
    # product on its own WABA is then configuration rather than a redesign.
    whatsapp_business_account_id: Mapped[str | None] = mapped_column(String(64))

    # Credentials stay in Secret Manager ([D19]). This names an override for a product
    # that needs its own token; NULL means the shared platform credential, which is the
    # right answer while one Meta App serves every product. Never a token value.
    whatsapp_access_token_secret_name: Mapped[str | None] = mapped_column(String(255))

    # ---------------------------------------------------------------- Calendar (§28, [D43])
    #
    # One Google Calendar per product, which is what lets every product book the same
    # wall-clock hour independently. NULL means this product has no calendar, and booking
    # is refused for it — never sent to another product's calendar, and there is no
    # platform-wide calendar to fall back to.
    google_calendar_id: Mapped[str | None] = mapped_column(String(255))

    # The refresh token stays in Secret Manager ([D19]); this only *names* an override, for
    # a product whose calendar sits in a different Google account. NULL means the shared
    # operations account's token, which is the approved setup. Never a token value.
    #
    # There is deliberately no client ID or client secret override. One OAuth client can be
    # consented by several Google accounts, each producing its own refresh token, so a
    # different account needs a different token — not a different client.
    google_calendar_token_secret_name: Mapped[str | None] = mapped_column(String(255))

    # The product's permanent meeting link (Option 2): created once in the operator's
    # own Google account and attached to every booked demo, because a service account
    # ([D62]) cannot generate Meet conferences itself. NULL means time-only
    # confirmations — never another product's room. A URL, never a secret.
    meeting_url: Mapped[str | None] = mapped_column(String(1024))

    # ---------------------------------------------------------------- slot policy ([D8], [D52])
    #
    # Sparse overrides of the global slot policy. NULL inherits the platform value, so a
    # product whose hours match everyone else's states nothing and duplicates nothing.
    # Named exactly as the settings they override, which lets one resolver read both.
    demo_duration_minutes: Mapped[int | None] = mapped_column(Integer)
    demo_buffer_minutes: Mapped[int | None] = mapped_column(Integer)
    demo_working_days: Mapped[list[str] | None] = mapped_column(JSONB(none_as_null=True))
    demo_working_hours_start: Mapped[str | None] = mapped_column(String(5))
    demo_working_hours_end: Mapped[str | None] = mapped_column(String(5))
    # NULL inherits the platform horizon. `-1` (`UNBOUNDED_BOOKING_HORIZON`) means no
    # artificial latest bookable day — distinct from NULL, which must keep inheriting.
    demo_booking_horizon_days: Mapped[int | None] = mapped_column(Integer)
    # NULL inherits. 0 = same day still offerable; 1 = earliest tomorrow.
    demo_min_lead_days: Mapped[int | None] = mapped_column(Integer)
    demo_timezone: Mapped[str | None] = mapped_column(String(64))

    # ---------------------------------------------------------------- audience ([D51], Phase 27)
    #
    # How this product's spreadsheets map onto the canonical contact fields ([D25]): a list
    # of `{"source", "field", "required"}` column mappings, validated by
    # `app.domain.audience.parse_audience_schema` before any file is read with it.
    #
    # NULL means no format has been confirmed, and uploads are refused. There is
    # deliberately no platform-wide mapping to fall back to: products receive spreadsheets
    # from different sources, and reading one with another's headers would put the wrong
    # column in the phone field without an error.
    audience_column_schema: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB(none_as_null=True)
    )

    # ---------------------------------------------------------------- conversion
    #
    # How interest becomes an action. `calendar_booking` (default) requires a product
    # calendar and exposes the booking tools. `external_registration` converts through a
    # shared registration URL and per-segment CTA copy — no calendar required.
    conversion_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default="calendar_booking", server_default="calendar_booking"
    )
    registration_url: Mapped[str | None] = mapped_column(Text)

    # ---------------------------------------------------------------- first reply
    #
    # The Meta-approved template sent once, instead of the AI text reply, when a
    # phone number messages this product for the first time. NULL (the placeholder
    # state) disables it: the turn runs the normal AI path. Like ownership, this
    # is runtime application data set through the first-reply API — template rows
    # are created by Meta sync, so no catalog or migration can know their IDs —
    # and never inferred. On template delete the reference clears (SET NULL)
    # rather than blocking the delete.
    first_reply_template_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        # Named as migration d4e5f6a7b8c9 created it, so metadata and a migrated
        # database agree (the naming convention would give a different name).
        ForeignKey(
            "whatsapp_templates.id",
            ondelete="SET NULL",
            name="fk_products_first_reply_template",
        ),
    )

    segments: Mapped[list[Segment]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    ai_configurations: Mapped[list[AIConfiguration]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # One number routes to one product, so two **active** products claiming the
        # same number would make inbound routing ambiguous — the exact thing [D40]
        # exists to prevent. Partial, because an inactive product may keep its old
        # number recorded and a product without WhatsApp has none at all.
        Index(
            "uq_products_active_whatsapp_number",
            "whatsapp_phone_number_id",
            unique=True,
            postgresql_where=text("is_active AND whatsapp_phone_number_id IS NOT NULL"),
        ),
        # One calendar, one product ([D43]). This is what makes the confirmed-slot rule on
        # `bookings (product_id, start_time)` equal to a per-calendar guarantee: if two
        # active products shared a calendar, each could confirm the same hour on it.
        # Partial for the same reasons as the number above.
        Index(
            "uq_products_active_google_calendar",
            "google_calendar_id",
            unique=True,
            postgresql_where=text("is_active AND google_calendar_id IS NOT NULL"),
        ),
        # JSONB holds whatever it is given. The application validates a mapping in full
        # before reading a file with it; this keeps out only the grossly wrong shapes.
        CheckConstraint(
            "audience_column_schema IS NULL OR jsonb_typeof(audience_column_schema) = 'array'",
            name="audience_column_schema_is_array",
        ),
        CheckConstraint(
            "conversion_mode IN ('calendar_booking', 'external_registration')",
            name="conversion_mode",
        ),
    )


class Segment(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A customer segment within one product (§28.2, [D49]).

    Ordinary rows, never a database enum: both SustainX and Life Herba expect to add
    segments, and a migration per segment is not acceptable. A product may have **zero**
    segments — Product 1 does — and nothing creates a placeholder to satisfy the schema.

    Segments exist for marketing targeting. Whether they also change AI behaviour is a
    separate question answered by `ai_configurations`, and for SustainX the answer is no.
    """

    __tablename__ = "segments"

    product_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)

    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Sparse CTA copy for external-registration products. NULL means inherit nothing —
    # the product prompt and registration_url still apply.
    cta_label: Mapped[str | None] = mapped_column(String(255))
    cta_instruction: Mapped[str | None] = mapped_column(Text)
    # This segment's own signup link, overriding the product's registration_url. NULL
    # inherits the product's.
    registration_url: Mapped[str | None] = mapped_column(Text)

    product: Mapped[Product] = relationship(back_populates="segments")

    __table_args__ = (
        # Unique within a product, not globally: two products may both have a
        # "government" segment and they are different things.
        UniqueConstraint("product_id", "slug", name="uq_segments_product_slug"),
        # The target of `ai_configurations`' composite foreign key, which is what stops
        # a configuration pairing one product with another product's segment. Redundant
        # with the primary key as a uniqueness claim; required as an FK target.
        UniqueConstraint("product_id", "id", name="uq_segments_product_id_id"),
        Index("ix_segments_product_id", "product_id"),
    )


class AIConfiguration(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Product AI defaults, and sparse segment overrides (§28.4).

    One row per product with `segment_id IS NULL` is that product's default. A row with
    a segment overrides it, and **every value column is nullable, where NULL means
    inherit**. A segment row therefore states only what differs — which is what lets
    Life Herba give a pharmacist a different system prompt without restating the
    retrieval parameters, and lets SustainX have no segment rows at all.

    Resolution is `COALESCE(segment row, product row)` per field. It is deliberately not
    implemented here: Phase 22 owns the runtime behaviour, and putting the rule in two
    places is how the two come to disagree.

    `qualification_required_fields` is JSONB rather than a delimited string because the
    difference between NULL and `[]` carries meaning: NULL inherits, while an empty list
    is a deliberate statement that no field is required — under which, per [D37], no
    lead can qualify. A text column cannot express both.
    """

    __tablename__ = "ai_configurations"

    product_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    # NULL identifies the product default. Non-NULL is an override for that segment.
    segment_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("segments.id", ondelete="CASCADE")
    )

    system_prompt: Mapped[str | None] = mapped_column(Text)
    qualification_required_fields: Mapped[list[Any] | None] = mapped_column(
        JSONB(none_as_null=True)
    )
    # NULL inherits. `required_fields` (default when unset everywhere) or `ai_judgement`.
    qualification_mode: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(255))

    rag_top_k: Mapped[int | None] = mapped_column(Integer)
    rag_min_similarity: Mapped[float | None] = mapped_column(Float)
    rag_max_context_chars: Mapped[int | None] = mapped_column(Integer)

    history_turns: Mapped[int | None] = mapped_column(Integer)
    max_tool_rounds: Mapped[int | None] = mapped_column(Integer)
    fallback_message: Mapped[str | None] = mapped_column(Text)

    product: Mapped[Product] = relationship(back_populates="ai_configurations")

    __table_args__ = (
        # Two partial unique indexes rather than one UNIQUE (product_id, segment_id).
        #
        # A plain unique constraint would not enforce the first rule at all: SQL treats
        # NULLs as distinct from one another, so a product could accumulate any number
        # of "default" rows and resolution would silently pick one. PostgreSQL 15 offers
        # NULLS NOT DISTINCT, but partial indexes work on every version and each one
        # states a separate rule in a form that can be read off the schema.
        #
        # At most one default per product.
        Index(
            "uq_ai_configurations_product_default",
            "product_id",
            unique=True,
            postgresql_where=text("segment_id IS NULL"),
        ),
        # At most one override per (product, segment).
        Index(
            "uq_ai_configurations_product_segment",
            "product_id",
            "segment_id",
            unique=True,
            postgresql_where=text("segment_id IS NOT NULL"),
        ),
        # A segment override must belong to its own product. Expressed as referential
        # integrity rather than application validation, so a configuration pairing one
        # product with another product's segment cannot be written at all.
        #
        # MATCH SIMPLE (the default) skips the check when any column is NULL, which is
        # exactly right: product-default rows carry no segment and are unaffected.
        ForeignKeyConstraint(
            ["product_id", "segment_id"],
            ["segments.product_id", "segments.id"],
            name="fk_ai_configurations_segment_within_product",
            ondelete="CASCADE",
        ),
        # Created by migration a1b2c3d4e5f6; declared here so the model and the schema agree.
        CheckConstraint(
            "qualification_mode IS NULL OR qualification_mode IN ('required_fields', 'ai_judgement')",
            name="qualification_mode",
        ),
    )
