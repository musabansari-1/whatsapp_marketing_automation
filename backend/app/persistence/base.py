"""Declarative base for ORM models.

Phase 1 defines the base only. Phase 2 adds the 19 tables from specification §15
(the 18 listed tables plus `whatsapp_suppressions` per [D16]) together with their
enums, indexes, unique constraints and the pgvector column.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base. Models arrive in Phase 2."""
