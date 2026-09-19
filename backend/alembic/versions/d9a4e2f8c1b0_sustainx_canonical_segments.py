"""Remap obsolete SustainX segment slugs onto the canonical launch set.

Phase-27 seeded ``government`` and ``industry``. Launch confirmed five segments:
``industries``, ``institutions``, ``government-bodies``, ``students``,
``professionals-executives``. Clear renames only:

* ``government`` → ``government-bodies``
* ``industry`` → ``industries``
* ``universities`` → ``institutions``

Ambiguous experimental segments (``corporates``, ``msmes``,
``developers-infrastructure``, ``manufacturing-industries``, and service / CoE /
training experiments) are intentionally left alone here. ``configure-products``
reports them for manual classification when they hold references.

This revision remaps references (audiences, campaigns, conversations, knowledge_bases)
from each obsolete row onto its canonical equivalent, drops the obsolete AI override,
and deletes the obsolete segment. If only the obsolete row exists it is renamed in
place. Other products are untouched. Idempotent. Does not activate SustainX.

Revision ID: d9a4e2f8c1b0
Revises: c8f3b12a9e01
Create Date: 2026-09-19
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

revision = "d9a4e2f8c1b0"
down_revision = "c8f3b12a9e01"
branch_labels = None
depends_on = None

RENAMES: tuple[tuple[str, str, str], ...] = (
    ("government", "government-bodies", "Government Bodies"),
    ("industry", "industries", "Industries"),
    ("universities", "institutions", "Institutions"),
)

FK_TABLES: tuple[str, ...] = (
    "audiences",
    "campaigns",
    "conversations",
    "knowledge_bases",
)


def upgrade() -> None:
    conn = op.get_bind()
    product_id = conn.execute(
        text("SELECT id FROM products WHERE slug = 'sustainx'")
    ).scalar()
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
            text(
                "UPDATE segments SET slug = :slug, display_name = :name "
                "WHERE id = :id"
            ),
            {"slug": canonical, "name": display_name, "id": old_id},
        )
        return

    if old_id == new_id:
        return

    for table in FK_TABLES:
        conn.execute(
            text(
                f"UPDATE {table} SET segment_id = :new_id "
                f"WHERE segment_id = :old_id"
            ),
            {"new_id": new_id, "old_id": old_id},
        )

    conn.execute(
        text("DELETE FROM ai_configurations WHERE segment_id = :old_id"),
        {"old_id": old_id},
    )
    conn.execute(text("DELETE FROM segments WHERE id = :old_id"), {"old_id": old_id})
