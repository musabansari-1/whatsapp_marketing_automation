"""Knowledge base persistence and vector retrieval (specification §11, [D1])."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.chunking import Chunk
from app.domain.enums import DocumentStatus, IngestionJobStatus
from app.domain.rag import RetrievedChunk, similarity_from_distance
from app.persistence.models import (
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIngestionJob,
)

# ---------------------------------------------------------------- knowledge bases


async def create_knowledge_base(
    session: AsyncSession, *, name: str, description: str | None
) -> KnowledgeBase:
    knowledge_base = KnowledgeBase(name=name, description=description)
    session.add(knowledge_base)
    await session.flush()
    return knowledge_base


async def get_knowledge_base(
    session: AsyncSession, knowledge_base_id: uuid.UUID
) -> KnowledgeBase | None:
    return await session.get(KnowledgeBase, knowledge_base_id)


async def list_knowledge_bases(session: AsyncSession) -> Sequence[KnowledgeBase]:
    result = await session.execute(select(KnowledgeBase).order_by(KnowledgeBase.name))
    return result.scalars().all()


# ---------------------------------------------------------------- documents


def content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


async def create_document(
    session: AsyncSession,
    *,
    knowledge_base_id: uuid.UUID,
    title: str,
    source_filename: str | None,
    content_type: str | None,
    byte_size: int,
    digest: str,
) -> KnowledgeDocument:
    """Record an uploaded document as PENDING.

    PENDING rather than READY: §11 makes ingestion asynchronous, and only a document
    whose chunks are embedded can be retrieved.
    """
    document = KnowledgeDocument(
        knowledge_base_id=knowledge_base_id,
        title=title,
        source_filename=source_filename,
        content_type=content_type,
        byte_size=byte_size,
        content_hash=digest,
        status=DocumentStatus.PENDING,
    )
    session.add(document)
    await session.flush()
    return document


async def get_document(session: AsyncSession, document_id: uuid.UUID) -> KnowledgeDocument | None:
    return await session.get(KnowledgeDocument, document_id)


async def find_by_hash(
    session: AsyncSession, *, knowledge_base_id: uuid.UUID, digest: str
) -> KnowledgeDocument | None:
    """Detect a re-upload of identical content.

    Re-ingesting the same bytes wastes embedding time and doubles every chunk in the
    retrieval pool, which skews results toward whatever happened to be uploaded twice.
    """
    result = await session.execute(
        select(KnowledgeDocument).where(
            KnowledgeDocument.knowledge_base_id == knowledge_base_id,
            KnowledgeDocument.content_hash == digest,
            KnowledgeDocument.status != DocumentStatus.ARCHIVED,
        )
    )
    return result.scalars().first()


async def list_documents(
    session: AsyncSession,
    knowledge_base_id: uuid.UUID,
    *,
    include_archived: bool = False,
) -> Sequence[KnowledgeDocument]:
    query = select(KnowledgeDocument).where(
        KnowledgeDocument.knowledge_base_id == knowledge_base_id
    )
    if not include_archived:
        query = query.where(KnowledgeDocument.status != DocumentStatus.ARCHIVED)
    result = await session.execute(query.order_by(KnowledgeDocument.created_at.desc()))
    return result.scalars().all()


async def archive_document(session: AsyncSession, document: KnowledgeDocument) -> None:
    """Archive a document so it stops being retrievable.

    Chunks are deliberately left in place rather than deleted: an answer already given
    cites chunk IDs in `ai_runs`, and destroying them would break that audit trail.
    Retrieval filters on document status, so archiving is enough to take the content
    out of circulation.
    """
    document.status = DocumentStatus.ARCHIVED
    document.archived_at = datetime.now(UTC)
    await session.flush()


async def mark_processing(session: AsyncSession, document: KnowledgeDocument) -> None:
    document.status = DocumentStatus.PROCESSING
    document.error_message = None
    await session.flush()


async def mark_ready(
    session: AsyncSession, document: KnowledgeDocument, *, chunk_count: int
) -> None:
    document.status = DocumentStatus.READY
    document.chunk_count = chunk_count
    document.error_message = None
    await session.flush()


async def mark_failed(session: AsyncSession, document: KnowledgeDocument, *, reason: str) -> None:
    """Record why ingestion failed.

    Stored on the document because §17 requires the dashboard to show ingestion
    errors; a failure only in the logs is invisible to the person who uploaded the
    file.
    """
    document.status = DocumentStatus.FAILED
    document.error_message = reason[:1000]
    await session.flush()


# ---------------------------------------------------------------- ingestion jobs


async def create_job(
    session: AsyncSession, *, document_id: uuid.UUID, task_id: str
) -> KnowledgeIngestionJob:
    job = KnowledgeIngestionJob(
        document_id=document_id, task_id=task_id, status=IngestionJobStatus.PENDING
    )
    session.add(job)
    await session.flush()
    return job


async def get_job_for_document(
    session: AsyncSession, document_id: uuid.UUID
) -> KnowledgeIngestionJob | None:
    result = await session.execute(
        select(KnowledgeIngestionJob)
        .where(KnowledgeIngestionJob.document_id == document_id)
        .order_by(KnowledgeIngestionJob.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()


async def start_job(session: AsyncSession, job: KnowledgeIngestionJob) -> None:
    job.status = IngestionJobStatus.PROCESSING
    job.attempts += 1
    job.started_at = datetime.now(UTC)
    await session.flush()


async def complete_job(
    session: AsyncSession, job: KnowledgeIngestionJob, *, chunks_created: int
) -> None:
    job.status = IngestionJobStatus.COMPLETED
    job.chunks_created = chunks_created
    job.completed_at = datetime.now(UTC)
    await session.flush()


async def fail_job(session: AsyncSession, job: KnowledgeIngestionJob, *, reason: str) -> None:
    job.status = IngestionJobStatus.FAILED
    job.error_message = reason[:1000]
    job.completed_at = datetime.now(UTC)
    await session.flush()


# ---------------------------------------------------------------- chunks


async def replace_chunks(
    session: AsyncSession,
    *,
    document: KnowledgeDocument,
    chunks: Sequence[Chunk],
    vectors: Sequence[Sequence[float]],
    embedding_model: str,
) -> int:
    """Store a document's chunks and their vectors, replacing any previous set.

    Replacing rather than appending is what makes re-ingestion idempotent (§20): a
    retried task produces the same chunks rather than a second copy of every one.

    The model that produced each vector is recorded alongside it, so [D1]'s
    prohibition on mixing vectors from incompatible models is enforceable rather than
    merely intended.
    """
    if len(chunks) != len(vectors):
        raise ValueError("chunk and vector counts must match")

    await session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id))

    for chunk, vector in zip(chunks, vectors, strict=True):
        session.add(
            KnowledgeChunk(
                document_id=document.id,
                chunk_index=chunk.index,
                content=chunk.content,
                token_count=chunk.estimated_tokens,
                embedding=list(vector),
                embedding_model=embedding_model,
            )
        )

    await session.flush()
    return len(chunks)


async def search(
    session: AsyncSession,
    *,
    knowledge_base_id: uuid.UUID,
    query_vector: Sequence[float],
    limit: int,
    embedding_model: str | None = None,
) -> list[RetrievedChunk]:
    """Nearest-neighbour search over a knowledge base's chunks (§11).

    Two filters are load-bearing:

    READY documents only — §11 says so, and a half-ingested or failed document would
    otherwise contribute partial content to answers.

    Matching embedding model — [D1] forbids comparing vectors from different models.
    Cosine distance between them is arithmetically valid and semantically meaningless,
    so an unfiltered search after a model change would return confident nonsense.
    """
    distance = KnowledgeChunk.embedding.cosine_distance(list(query_vector))

    query = (
        select(
            KnowledgeChunk.id,
            KnowledgeChunk.document_id,
            KnowledgeDocument.title,
            KnowledgeChunk.content,
            distance.label("distance"),
        )
        .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
        .where(
            KnowledgeDocument.knowledge_base_id == knowledge_base_id,
            KnowledgeDocument.status == DocumentStatus.READY,
            KnowledgeChunk.embedding.is_not(None),
        )
    )
    if embedding_model is not None:
        query = query.where(KnowledgeChunk.embedding_model == embedding_model)

    result = await session.execute(query.order_by(distance).limit(limit))

    return [
        RetrievedChunk(
            chunk_id=str(row[0]),
            document_id=str(row[1]),
            document_title=row[2],
            content=row[3],
            similarity=similarity_from_distance(float(row[4])),
        )
        for row in result.all()
    ]


async def count_chunks(session: AsyncSession, document_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(KnowledgeChunk)
        .where(KnowledgeChunk.document_id == document_id)
    )
    return int(result.scalar_one())
