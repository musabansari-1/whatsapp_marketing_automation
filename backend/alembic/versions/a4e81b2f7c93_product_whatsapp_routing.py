"""Product WhatsApp routing, and the end of productless rows (Phase 23, [D40])

One Meta App and **one shared WABA**, with a phone number per product. The number is
therefore what identifies a product — the WABA identifies nothing, since every product
shares it — and `metadata.phone_number_id` becomes the inbound routing key.

Two active products must never claim one number, or inbound routing would be ambiguous.
Enforced by a partial unique index rather than a plain one: an inactive product may keep
its old number recorded, and a product that does not use WhatsApp has none at all.

**Four nullability debts close here**, because every path that creates these rows can now
supply a product: inbound routing resolves the lead's, the conversation takes the lead's,
a message takes its campaign's or conversation's, and an AI run takes its conversation's.

`whatsapp_templates.product_id` deliberately does **not** close. A shared WABA returns
every product's templates in one undifferentiated list and Meta's records carry no
product field, so ownership cannot be inferred without inventing a mapping. That is a
decision, not a migration.

**Backfill refuses rather than guesses.** Any lead, conversation, message or AI run
without a product aborts the migration with a message naming what was found. There is no
"assign the historical product": nothing in the data proves a productless row belongs to
Academicae, and quietly deciding that it does is how a customer ends up in the wrong
product's conversation.

Revision ID: a4e81b2f7c93
Revises: f2a9c31e7b45
Create Date: 2026-09-10
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a4e81b2f7c93"
down_revision = "f2a9c31e7b45"
branch_labels = None
depends_on = None

#: Tables whose product becomes mandatory here, in dependency order.
CLOSING = ("leads", "conversations", "messages", "ai_runs")


def upgrade() -> None:
    # ---------------------------------------------------------------- product config
    op.add_column("products", sa.Column("whatsapp_phone_number_id", sa.String(64), nullable=True))
    op.add_column(
        "products", sa.Column("whatsapp_business_account_id", sa.String(64), nullable=True)
    )
    op.add_column(
        "products", sa.Column("whatsapp_access_token_secret_name", sa.String(255), nullable=True)
    )

    # One number, one product. Partial so inactive products and non-WhatsApp products
    # are unaffected.
    op.create_index(
        "uq_products_active_whatsapp_number",
        "products",
        ["whatsapp_phone_number_id"],
        unique=True,
        postgresql_where=sa.text("is_active AND whatsapp_phone_number_id IS NOT NULL"),
    )

    # ---------------------------------------------------------------- prove, then close
    connection = op.get_bind()
    for table in CLOSING:
        orphaned = connection.execute(
            sa.text(f"SELECT count(*) FROM {table} WHERE product_id IS NULL")  # noqa: S608
        ).scalar_one()
        if orphaned:
            raise RuntimeError(
                f"{orphaned} row(s) in {table!r} have no product. Phase 23 will not "
                "guess one: attribute them deliberately, then re-run this migration. "
                "Assigning them to the historical product because it came first is "
                "exactly the inference [D40] and [D45] forbid."
            )

    for table in CLOSING:
        op.alter_column(table, "product_id", existing_type=sa.UUID(), nullable=False)

    # ---------------------------------------------------------------- lead identity
    #
    # The transitional lane is gone: no path creates a lead without a product any more,
    # so the index covering `product_id IS NULL` has nothing left to protect.
    op.drop_index("uq_leads_transitional_phone", table_name="leads")

    # `uq_leads_product_phone` stays as it is. Its predicate is now always true, but
    # rebuilding a unique index over live data to remove a redundant WHERE buys nothing
    # and risks a lock; Phase 27 can tidy it if it ever matters.


def downgrade() -> None:
    """Restore the previous schema.

    **Not the previous data.** Re-widening these columns is safe, but the productless
    rows they once permitted cannot be recovered — nothing recorded which rows had no
    product before the backfill, because by then none did.

    Recreating `uq_leads_transitional_phone` succeeds only while no two products hold the
    same number with a NULL product, which after this migration is vacuously true.
    """
    op.create_index(
        "uq_leads_transitional_phone",
        "leads",
        ["phone_e164"],
        unique=True,
        postgresql_where=sa.text("product_id IS NULL"),
    )

    for table in reversed(CLOSING):
        op.alter_column(table, "product_id", existing_type=sa.UUID(), nullable=True)

    op.drop_index("uq_products_active_whatsapp_number", table_name="products")
    op.drop_column("products", "whatsapp_access_token_secret_name")
    op.drop_column("products", "whatsapp_business_account_id")
    op.drop_column("products", "whatsapp_phone_number_id")
