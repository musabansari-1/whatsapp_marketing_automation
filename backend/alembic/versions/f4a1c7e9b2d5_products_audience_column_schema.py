"""Per-product audience column schemas (Phase 27, [D51])

Adds `products.audience_column_schema`: how one product's spreadsheets map onto the
canonical contact fields ([D25]). NULL means no format has been confirmed for that
product, and its uploads are refused.

**Schema only, no data.** Which headers a product's spreadsheets carry is business
configuration, not schema. It is written by `python -m app.cli configure-products` or, for
the original product, copied from the pre-Phase-27 environment by
`python -m app.cli import-legacy-config` — never by a migration, which would have to read
deployment settings or invent a mapping.

The column is JSONB and the application validates every mapping in full before a file is
read with it. A check constraint keeps out only the grossly wrong shapes: anything that is
not an array.

Revision ID: f4a1c7e9b2d5
Revises: e8a3c5d2f1b7
Create Date: 2026-09-12
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f4a1c7e9b2d5"
down_revision = "e8a3c5d2f1b7"
branch_labels = None
depends_on = None

CHECK_NAME = "ck_products_audience_column_schema_is_array"


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("audience_column_schema", postgresql.JSONB(), nullable=True),
    )
    op.create_check_constraint(
        op.f(CHECK_NAME),
        "products",
        "audience_column_schema IS NULL OR jsonb_typeof(audience_column_schema) = 'array'",
    )


def downgrade() -> None:
    """Remove the column — **only while no product has a schema configured.**

    A configured mapping is a confirmed client format. Dropping it would lose it silently,
    and the code a downgrade returns to reads one environment mapping for every product
    instead, so carrying the value back is a deliberate operator act. To roll back, clear
    the column first: `UPDATE products SET audience_column_schema = NULL`.
    """
    configured = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM products WHERE audience_column_schema IS NOT NULL"))
        .scalar_one()
    )
    if configured:
        raise RuntimeError(
            f"{configured} product(s) have an audience column schema configured. The "
            "downgrade will not discard a confirmed spreadsheet format; record it, clear "
            "the column, then re-run the downgrade."
        )

    op.drop_constraint(op.f(CHECK_NAME), "products", type_="check")
    op.drop_column("products", "audience_column_schema")
