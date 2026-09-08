"""Product-scoped lead identity (Phase 19, [D41])

**This is a one-way door.** Once two products hold the same phone number, there is no
correct way back: collapsing them would have to decide which product's status,
qualification and conversation history survive, and nothing in the data can answer that.
`downgrade()` restores the schema, not the possibility — see the note there.

A lead becomes `(product, phone)` rather than `phone` alone. The same human reachable on
one number is a separate lead in each product, so their SustainX qualification cannot
overwrite their Life Herba qualification. Within one product that number is still exactly
one lead, however many audiences and segments it belongs to.

**Two partial unique indexes, not one plain unique.** `product_id` stays nullable until
Phase 23, because the inbound webhook path cannot resolve a product until routing on
`metadata.phone_number_id` exists and [D45] forbids guessing one. A plain
`UNIQUE (product_id, phone_e164)` over a nullable column would enforce nothing for the
rows that most need it: SQL treats NULLs as distinct, so the transitional lane could
accumulate any number of duplicate leads for one number.

    product-known    UNIQUE (product_id, phone_e164) WHERE product_id IS NOT NULL
    transitional     UNIQUE (phone_e164)             WHERE product_id IS NULL

The second is scaffolding. Phase 23 removes it, along with the nullability, once every
lead creation path can supply a product.

`audiences` and `campaigns` become NOT NULL here: their write paths now resolve a product
(§28, Phase 19), which is the condition Phase 18 was waiting on.

Revision ID: f2a9c31e7b45
Revises: e5c74a1b8d09
Create Date: 2026-09-08
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "f2a9c31e7b45"
down_revision = "e5c74a1b8d09"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------------------------------------------------------------- leads
    #
    # The global rule goes first: the two partial indexes below replace it, and holding
    # both at once would refuse a phone that legitimately belongs to two products.
    op.drop_constraint("uq_leads_phone_e164", "leads", type_="unique")

    op.create_index(
        "uq_leads_product_phone",
        "leads",
        ["product_id", "phone_e164"],
        unique=True,
        postgresql_where=sa.text("product_id IS NOT NULL"),
    )

    # Transitional. Removed in Phase 23 with the nullability it exists to cover.
    op.create_index(
        "uq_leads_transitional_phone",
        "leads",
        ["phone_e164"],
        unique=True,
        postgresql_where=sa.text("product_id IS NULL"),
    )

    # ---------------------------------------------------------------- scope
    #
    # Phase 18 left these nullable because nothing could fill them. Audience upload now
    # resolves a product and campaigns take theirs from their audience, so the rows are
    # all attributed and the constraint can close.
    op.alter_column("audiences", "product_id", existing_type=sa.UUID(), nullable=False)
    op.alter_column("campaigns", "product_id", existing_type=sa.UUID(), nullable=False)


def downgrade() -> None:
    """Restore the previous schema.

    **Not always the previous data.** Re-adding a global unique on `phone_e164` fails if
    two products already hold the same number, and that failure is correct: the rows are
    genuinely different leads with their own status, qualification and conversations, and
    silently discarding one would lose customer state.

    Reversible in practice only while at most one product has leads — which is the state
    this migration is applied in, and the boundary a test pins.
    """
    op.alter_column("campaigns", "product_id", existing_type=sa.UUID(), nullable=True)
    op.alter_column("audiences", "product_id", existing_type=sa.UUID(), nullable=True)

    op.drop_index("uq_leads_transitional_phone", table_name="leads")
    op.drop_index("uq_leads_product_phone", table_name="leads")

    op.create_unique_constraint("uq_leads_phone_e164", "leads", ["phone_e164"])
