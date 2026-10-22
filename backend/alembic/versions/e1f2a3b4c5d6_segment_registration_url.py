"""Per-segment registration URL.

An external-registration product may send each audience to its own signup link rather
than one shared destination: Life Herba has `/join/doctor`, `/join/pharmacy`,
`/join/clinic` and `/join/wellness-center`, each opening that role's signup in the app
(or the Play Store first). This revision adds:

- ``segments.registration_url`` — NULL inherits ``products.registration_url``.

Revision ID: e1f2a3b4c5d6
Revises: d4e5f6a7b8c9
Create Date: 2026-10-22
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "e1f2a3b4c5d6"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("segments", sa.Column("registration_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("segments", "registration_url")
