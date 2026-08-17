"""Declarative base for ORM models.

Deliberately imports nothing from `app.persistence.models`: the models import this
module, so importing them back here would be circular. Anything that needs populated
metadata — Alembic, the test schema fixtures — imports `app.persistence.models`
explicitly to register the tables.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Predictable constraint and index names. Without this, SQLAlchemy leaves some
# constraint names to PostgreSQL, and Alembic then cannot reliably drop or alter
# them in a later migration.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Shared declarative base for all models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
