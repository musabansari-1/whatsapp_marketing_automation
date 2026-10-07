"""Per-product first-reply template.

Adds `products.first_reply_template_id`: the Meta-approved template sent once,
instead of the AI text reply, when a phone number messages a product for the
first time. NULL (the placeholder state) disables it — the turn runs the normal
AI path — so no product changes behaviour until an operator assigns a real
template and selects it here. Values arrive via the first-reply API, never via
migration: template rows are created by Meta sync, so a migration cannot know
their IDs.

The foreign key is SET NULL on delete: removing a template must disable the
welcome, never block the delete or leave a dangling reference behind.

Revision ID: d4e5f6a7b8c9
Revises: a3b4c5d6e7f8
Create Date: 2026-10-06
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "d4e5f6a7b8c9"
down_revision = "a3b4c5d6e7f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "products",
        # Nullable: absent means no welcome template, never another product's.
        sa.Column("first_reply_template_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_products_first_reply_template",
        "products",
        "whatsapp_templates",
        ["first_reply_template_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_products_first_reply_template", "products", type_="foreignkey")
    op.drop_column("products", "first_reply_template_id")
