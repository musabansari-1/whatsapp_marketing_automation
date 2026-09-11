"""Per-product calendars, and bookings owned by a product (Phase 24, [D43], [D52])

**Each product books into its own Google Calendar.** One shared operations account holds
access to all of them, so the OAuth client and refresh token stay shared configuration;
what differs is the calendar ID, recorded on the product. There is no platform-wide
calendar: a product without one refuses bookings.

**Products gain sparse slot-policy overrides.** Every one is NULL, meaning "inherit the
platform value", so no product duplicates hours it shares with the rest.

**`bookings.product_id` becomes NOT NULL — derived, never guessed.** A booking belongs to
the product of the conversation it was made in (§28.5). Rows written between Phase 18 and
now carry no product, because `record_confirmed_booking` did not set one, so they are
filled from their conversation, or from their lead when the conversation has since been
deleted. That is not inference: both are the booking's own parents, and both are NOT NULL
since Phase 23.

It aborts, naming what it found, if the parents disagree — a booking already filed under
one product whose conversation or lead belongs to another, or a conversation and lead that
point at different products. Nothing in the data says which is right, so nothing picks.

**The confirmed-slot rule becomes per product.** `UNIQUE (start_time) WHERE CONFIRMED`
becomes `UNIQUE (product_id, start_time) WHERE CONFIRMED`, so every product may confirm the
same hour on its own calendar while a second confirmation of that hour within one product
still cannot commit. A partial unique index on active products' calendar IDs keeps "per
product" equal to "per calendar".

Revision ID: c9e4a7b2d1f6
Revises: b3c8d1e5f927
Create Date: 2026-09-11
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c9e4a7b2d1f6"
down_revision = "b3c8d1e5f927"
branch_labels = None
depends_on = None

#: Nullable overrides of the [D8] settings of the same name.
SLOT_OVERRIDES: tuple[tuple[str, sa.types.TypeEngine[object]], ...] = (
    ("demo_duration_minutes", sa.Integer()),
    ("demo_buffer_minutes", sa.Integer()),
    ("demo_working_days", postgresql.JSONB()),
    ("demo_working_hours_start", sa.String(5)),
    ("demo_working_hours_end", sa.String(5)),
    ("demo_booking_horizon_days", sa.Integer()),
    ("demo_timezone", sa.String(64)),
)

#: Each query counts bookings whose product cannot be established without choosing.
DISAGREEMENTS: tuple[tuple[str, str], ...] = (
    (
        "a booking filed under a different product from its conversation",
        """
        SELECT count(*) FROM bookings b JOIN conversations c ON c.id = b.conversation_id
         WHERE b.product_id IS NOT NULL AND b.product_id <> c.product_id
        """,
    ),
    (
        "a booking filed under a different product from its lead",
        """
        SELECT count(*) FROM bookings b JOIN leads l ON l.id = b.lead_id
         WHERE b.product_id IS NOT NULL AND b.product_id <> l.product_id
        """,
    ),
    (
        "an unfiled booking whose conversation and lead belong to different products",
        """
        SELECT count(*) FROM bookings b
          JOIN conversations c ON c.id = b.conversation_id
          JOIN leads l ON l.id = b.lead_id
         WHERE b.product_id IS NULL AND c.product_id <> l.product_id
        """,
    ),
)


def upgrade() -> None:
    # ---------------------------------------------------------------- products
    op.add_column("products", sa.Column("google_calendar_id", sa.String(255), nullable=True))
    op.add_column(
        "products",
        sa.Column("google_calendar_token_secret_name", sa.String(255), nullable=True),
    )
    for name, column_type in SLOT_OVERRIDES:
        op.add_column("products", sa.Column(name, column_type, nullable=True))

    op.create_index(
        "uq_products_active_google_calendar",
        "products",
        ["google_calendar_id"],
        unique=True,
        postgresql_where=sa.text("is_active AND google_calendar_id IS NOT NULL"),
    )

    # ---------------------------------------------------------------- bookings: prove
    connection = op.get_bind()
    for description, query in DISAGREEMENTS:
        found = connection.execute(sa.text(query)).scalar_one()
        if found:
            raise RuntimeError(
                f"{found} booking(s) found with {description}. Phase 24 will not choose "
                "between them: correct the rows deliberately, then re-run this migration."
            )

    # ---------------------------------------------------------------- bookings: derive
    #
    # From the booking's own parents, conversation first because it is what the booking
    # was made in, then the lead where the conversation link was cleared by a deletion.
    op.execute(
        """
        UPDATE bookings b SET product_id = c.product_id
          FROM conversations c
         WHERE b.product_id IS NULL AND c.id = b.conversation_id
        """
    )
    op.execute(
        """
        UPDATE bookings b SET product_id = l.product_id
          FROM leads l
         WHERE b.product_id IS NULL AND l.id = b.lead_id
        """
    )

    unfiled = connection.execute(
        sa.text("SELECT count(*) FROM bookings WHERE product_id IS NULL")
    ).scalar_one()
    if unfiled:  # pragma: no cover - lead_id is NOT NULL, so every booking has a lead
        raise RuntimeError(f"{unfiled} booking(s) still have no product after derivation.")

    op.alter_column("bookings", "product_id", existing_type=sa.UUID(), nullable=False)

    # ---------------------------------------------------------------- bookings: slots
    op.drop_index("uq_bookings_confirmed_start_time", table_name="bookings")
    op.create_index(
        "uq_bookings_product_confirmed_start_time",
        "bookings",
        ["product_id", "start_time"],
        unique=True,
        postgresql_where=sa.text("status = 'CONFIRMED'"),
    )


def downgrade() -> None:
    """Restore the previous schema — **while the data still fits it.**

    The global confirmed-slot rule cannot come back once two products have confirmed the
    same hour, which is exactly what this phase exists to allow. The downgrade then fails
    with an integrity error and changes nothing. It never cancels or deletes a booking to
    make room: which product's customer loses their demo is not a decision a downgrade can
    make.

    Products lose their calendar and slot-policy columns, and with them the only record of
    which calendar each product used. The product IDs derived onto bookings stay, since
    re-widening a column does not require forgetting a correct value.
    """
    op.create_index(
        "uq_bookings_confirmed_start_time",
        "bookings",
        ["start_time"],
        unique=True,
        postgresql_where=sa.text("status = 'CONFIRMED'"),
    )
    op.drop_index("uq_bookings_product_confirmed_start_time", table_name="bookings")
    op.alter_column("bookings", "product_id", existing_type=sa.UUID(), nullable=True)

    op.drop_index("uq_products_active_google_calendar", table_name="products")
    for name, _column_type in reversed(SLOT_OVERRIDES):
        op.drop_column("products", name)
    op.drop_column("products", "google_calendar_token_secret_name")
    op.drop_column("products", "google_calendar_id")
