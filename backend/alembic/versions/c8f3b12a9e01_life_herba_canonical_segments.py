"""Remap obsolete Life Herba segment slugs onto the canonical launch set.

Phase-27 seeded ``healthcare-professionals`` and ``wellness-centers``. Launch confirmed
``healthcare-practitioners`` and ``wellness-centres``. The catalog creates by slug and
never deletes, so both can coexist.

This revision remaps references (audiences, campaigns, conversations, knowledge_bases)
from each obsolete row onto its canonical equivalent, drops the obsolete AI override,
and deletes the obsolete segment. If only the obsolete row exists it is renamed in
place. Other products are untouched. Idempotent. Does not activate Life Herba.

Revision ID: c8f3b12a9e01
Revises: b7e2a91c4d08
Create Date: 2026-09-18
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

revision = "c8f3b12a9e01"
down_revision = "b7e2a91c4d08"
branch_labels = None
depends_on = None

RENAMES: tuple[tuple[str, str, str], ...] = (
    ("healthcare-professionals", "healthcare-practitioners", "Healthcare Practitioners"),
    ("wellness-centers", "wellness-centres", "Wellness Centres"),
)

FK_TABLES: tuple[str, ...] = (
    "audiences",
    "campaigns",
    "conversations",
    "knowledge_bases",
)


def upgrade() -> None:
    conn = op.get_bind()
    product_id = conn.execute(text("SELECT id FROM products WHERE slug = 'life-herba'")).scalar()
    if product_id is None:
        return

    for obsolete, canonical, display_name in RENAMES:
        _reconcile(conn, product_id, obsolete, canonical, display_name)


def downgrade() -> None:
    """No reverse rename.

    Conversations and audiences may already point at the canonical slug; inventing the
    obsolete rows again would recreate the duplicate state this revision removes.
    """
    return


def _reconcile(
    conn: sa.Connection,
    product_id: object,
    obsolete: str,
    canonical: str,
    display_name: str,
) -> None:
    old_id = conn.execute(
        text("SELECT id FROM segments WHERE product_id = :pid AND slug = :slug"),
        {"pid": product_id, "slug": obsolete},
    ).scalar()
    if old_id is None:
        return

    new_id = conn.execute(
        text("SELECT id FROM segments WHERE product_id = :pid AND slug = :slug"),
        {"pid": product_id, "slug": canonical},
    ).scalar()

    if new_id is None:
        conn.execute(
            text("UPDATE segments SET slug = :slug, display_name = :name WHERE id = :id"),
            {"slug": canonical, "name": display_name, "id": old_id},
        )
        return

    if old_id == new_id:
        return

    for table in FK_TABLES:
        conn.execute(
            # Table names come from the fixed FK_TABLES tuple, never from input.
            text(f"UPDATE {table} SET segment_id = :new_id WHERE segment_id = :old_id"),  # noqa: S608
            {"new_id": new_id, "old_id": old_id},
        )

    conn.execute(
        text("DELETE FROM ai_configurations WHERE segment_id = :old_id"),
        {"old_id": old_id},
    )
    conn.execute(text("DELETE FROM segments WHERE id = :old_id"), {"old_id": old_id})
