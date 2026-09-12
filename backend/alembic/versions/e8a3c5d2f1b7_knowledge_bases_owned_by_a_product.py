"""Knowledge bases owned by a product; names unique per product (Phase 26, [D56])

**`knowledge_bases.product_id` becomes NOT NULL.** Every creation path is now inside a
product: the dashboard API creates knowledge bases under `/p/{product_slug}`, and nothing
else writes them. This was the last product nullability debt.

**It proves before it closes.** Phase 18 backfilled every knowledge base that existed then
to Academicae. One created afterwards through the old unscoped endpoint has no product, and
this migration will not give it one: a knowledge base has no parent to derive an owner
from, and which product's assistant should answer from it is exactly the decision [D56]
forbids guessing. It aborts, naming how many it found.

**Names become unique per product.** `UNIQUE (name)` becomes `UNIQUE (product_id, name)`,
so two products may each keep a "Product FAQs". Deferred from Phase 18 on purpose: over a
nullable column a composite unique would have let two unowned bases share a name, and
swapping the rules only once the column is NOT NULL means no moment where the guarantee is
weaker than before.

Revision ID: e8a3c5d2f1b7
Revises: c9e4a7b2d1f6
Create Date: 2026-09-11
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "e8a3c5d2f1b7"
down_revision = "c9e4a7b2d1f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    unowned = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM knowledge_bases WHERE product_id IS NULL"))
        .scalar_one()
    )
    if unowned:
        raise RuntimeError(
            f"{unowned} knowledge base(s) have no product. Phase 26 will not choose one: "
            "which product's assistant answers from a document is not something the data "
            "can say. Attribute them deliberately, then re-run this migration."
        )

    op.alter_column("knowledge_bases", "product_id", existing_type=sa.UUID(), nullable=False)

    op.drop_constraint("uq_knowledge_bases_name", "knowledge_bases", type_="unique")
    op.create_unique_constraint(
        "uq_knowledge_bases_product_name", "knowledge_bases", ["product_id", "name"]
    )


def downgrade() -> None:
    """Restore the global name rule — **while the data still fits it.**

    Once two products each hold a knowledge base of the same name, which this phase
    exists to allow, the global rule cannot come back: the downgrade fails with an
    integrity error and changes nothing. It never renames or deletes a knowledge base to
    make room, because either would change what an assistant answers from.
    """
    op.create_unique_constraint("uq_knowledge_bases_name", "knowledge_bases", ["name"])
    op.drop_constraint("uq_knowledge_bases_product_name", "knowledge_bases", type_="unique")
    op.alter_column("knowledge_bases", "product_id", existing_type=sa.UUID(), nullable=True)
