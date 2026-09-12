"""RAG knowledge base (§11, §15, [D1]).

The vector column width comes from configuration, not a literal. [D1] fixes it at
migration time and forbids mixing vectors from incompatible models, so
`knowledge_chunks` records the model that produced each vector alongside it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import get_settings
from app.domain.enums import DocumentStatus, IngestionJobStatus
from app.persistence.base import Base
from app.persistence.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    pass

# Read once at import so the mapped column and the migration agree. Changing this
# is a migration plus a full re-embed ([D1]) — never an in-place edit.
EMBEDDING_DIMENSION = get_settings().embedding_dimension


class KnowledgeBase(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A named collection of approved business documents (§11)."""

    __tablename__ = "knowledge_bases"

    # §28 product scope, NOT NULL since Phase 26: every creation path is inside a
    # product, and [D56] makes an unowned base unsafe to retrieve from anyway.
    product_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    # Nullable by design, permanently: §28 allows a product-wide entity, and a
    # product may have no segments at all. Never filled with a placeholder.
    segment_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("segments.id", ondelete="RESTRICT")
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    documents: Mapped[list[KnowledgeDocument]] = relationship(
        back_populates="knowledge_base", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # Names are unique within a product, not across the platform: two products may
        # each keep a "Product FAQs". The global rule this replaced was deferred from
        # Phase 18 until every writer supplied a product, because over a nullable column
        # a composite unique would have let two unowned bases share a name.
        UniqueConstraint("product_id", "name", name="uq_knowledge_bases_product_name"),
        Index("ix_knowledge_bases_product_id", "product_id"),
        Index("ix_knowledge_bases_segment_id", "segment_id"),
        # A segment must belong to this row's own product (§28.8). MATCH SIMPLE
        # skips the check when either column is NULL, so a product-wide row is
        # unaffected.
        ForeignKeyConstraint(
            ["product_id", "segment_id"],
            ["segments.product_id", "segments.id"],
            name="fk_knowledge_bases_segment_within_product",
            ondelete="RESTRICT",
        ),
    )


class KnowledgeDocument(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """An uploaded document.

    Contents are untrusted data (§11). Nothing in a document may be treated as an
    instruction, which is a retrieval-time and prompt-construction concern (Phase 8
    and 10) rather than something the schema can enforce — noted here so it is not
    forgotten.

    Only READY documents are eligible for retrieval (§11).
    """

    __tablename__ = "knowledge_documents"

    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )

    title: Mapped[str] = mapped_column(String(512), nullable=False)
    source_filename: Mapped[str | None] = mapped_column(String(512))
    content_type: Mapped[str | None] = mapped_column(String(128))
    byte_size: Mapped[int | None] = mapped_column(Integer)
    # Detects a re-upload of identical content.
    content_hash: Mapped[str | None] = mapped_column(String(64))

    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, name="document_status", native_enum=True),
        nullable=False,
        default=DocumentStatus.PENDING,
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    knowledge_base: Mapped[KnowledgeBase] = relationship(back_populates="documents")
    chunks: Mapped[list[KnowledgeChunk]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    ingestion_jobs: Mapped[list[KnowledgeIngestionJob]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_knowledge_documents_kb_id", "knowledge_base_id"),
        # Retrieval filters to READY documents within a knowledge base.
        Index("ix_knowledge_documents_kb_status", "knowledge_base_id", "status"),
    )


class KnowledgeChunk(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A chunk of document text and its embedding.

    `embedding_model` is stored per row so vectors are attributable. [D1] forbids
    mixing vectors from incompatible models; recording the producer is what lets
    Phase 8 detect and refuse a mismatch rather than silently returning nonsense.
    """

    __tablename__ = "knowledge_chunks"

    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int | None] = mapped_column(Integer)

    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSION))
    embedding_model: Mapped[str | None] = mapped_column(String(255))

    chunk_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    document: Mapped[KnowledgeDocument] = relationship(back_populates="chunks")

    __table_args__ = (
        # Re-running ingestion for a document must overwrite, not duplicate (§20).
        UniqueConstraint("document_id", "chunk_index", name="uq_knowledge_chunks_doc_index"),
        Index("ix_knowledge_chunks_document_id", "document_id"),
        # HNSW over cosine distance. Chosen over IVFFlat because it needs no training
        # pass and stays accurate as the corpus grows, which suits a knowledge base
        # that is written rarely and queried on every AI turn.
        Index(
            "ix_knowledge_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class KnowledgeIngestionJob(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One ingestion run for a document (§11).

    `task_id` is unique so a redelivered Cloud Task is recognized as a repeat rather
    than re-embedding the document (§20).
    """

    __tablename__ = "knowledge_ingestion_jobs"

    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[IngestionJobStatus] = mapped_column(
        Enum(IngestionJobStatus, name="ingestion_job_status", native_enum=True),
        nullable=False,
        default=IngestionJobStatus.PENDING,
    )

    task_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunks_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)

    document: Mapped[KnowledgeDocument] = relationship(back_populates="ingestion_jobs")

    __table_args__ = (
        Index("ix_knowledge_ingestion_jobs_document_id", "document_id"),
        Index("ix_knowledge_ingestion_jobs_status", "status"),
    )
