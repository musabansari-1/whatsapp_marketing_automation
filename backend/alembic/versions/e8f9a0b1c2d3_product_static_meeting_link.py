"""Per-product static meeting link (Option 2).

Adds `products.meeting_url`: the permanent Google Meet link an operator creates in
their own Google account for one product's demos. A service account ([D62]) cannot
generate Meet conferences itself — Google answers `400 Invalid conference type
value` — so the platform attaches this fixed link instead of asking Google for one.

Nullable and never defaulted: a product without a link still books (time-only
confirmation), and inventing a placeholder URL here would send customers to a room
that does not exist. Values arrive via `configure-products`, never via migration.

Revision ID: e8f9a0b1c2d3
Revises: 7d2e5a91c4f3
Create Date: 2026-09-27
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "e8f9a0b1c2d3"
down_revision = "7d2e5a91c4f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "products",
        # Nullable: absent means time-only confirmations, never another product's
        # room. 1024 matches bookings.meeting_link, which stores this per booking.
        sa.Column("meeting_url", sa.String(length=1024), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("products", "meeting_url")
