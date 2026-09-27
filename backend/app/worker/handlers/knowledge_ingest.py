"""Knowledge document ingestion (specification §11, §20, [D1]).

The §11 pipeline, in the worker because embedding is slow and §4 forbids slow work in
a request:

    parse → clean → chunk → embed → store → READY

Idempotent (§20): a retried task replaces the document's chunks rather than appending,
so re-running it converges instead of doubling the retrieval pool.

Failures are recorded on the document as well as raised, because §17 requires the
dashboard to surface ingestion errors — a failure only in the logs is invisible to the
person who uploaded the file.
"""

from __future__ import annotations

import uuid

from app.core.config import Settings, get_settings
from app.core.correlation import bind_log_context
from app.core.errors import ProviderError
from app.core.logging import get_logger
from app.domain.chunking import ChunkingPolicy, chunk_text
from app.domain.documents import DocumentParseError, parse_document
from app.integrations.queue import TaskRequest
from app.integrations.registry import build_embedding_provider
from app.persistence.database import get_session_factory
from app.persistence.repositories import knowledge as knowledge_repo
from app.worker.handlers.campaign_send import RetryableSendError

logger = get_logger(__name__)


def chunking_policy_from(settings: Settings) -> ChunkingPolicy:
    return ChunkingPolicy(
        max_chars=settings.rag_chunk_chars,
        overlap_chars=settings.rag_chunk_overlap_chars,
    )


async def ingest_knowledge_document(task: TaskRequest) -> dict[str, int | str]:
    """Parse, chunk, embed and store one document.

    The uploaded bytes travel in the task payload rather than being re-read from
    storage, because V1 has no blob store — noted as a limitation rather than hidden.
    """
    settings = get_settings()
    document_id = task.payload.get("document_id")
    if not document_id:
        raise ValueError("document_id missing from task payload")

    content = task.payload.get("content")
    if not isinstance(content, str):
        raise ValueError("content missing from task payload")

    import base64

    raw = base64.b64decode(content)
    factory = get_session_factory()

    async with factory() as session:
        document = await knowledge_repo.get_document(session, uuid.UUID(str(document_id)))
        if document is None:
            # Deleted after the task was queued (per-document hard delete). Terminal:
            # retrying would fail the same way forever, and there is no row to record
            # anything on.
            logger.info("knowledge_ingest_skipped", reason="document_deleted")
            return {"result": "deleted_before_ingest", "document_id": str(document_id)}

        bind_log_context(document_id=str(document.id))
        job = await knowledge_repo.get_job_for_document(session, document.id)

        await knowledge_repo.mark_processing(session, document)
        if job is not None:
            await knowledge_repo.start_job(session, job)
        await session.commit()

        filename = document.source_filename or document.title

    try:
        parsed = parse_document(raw, filename=filename)
        chunks = chunk_text(parsed.text, policy=chunking_policy_from(settings))

        if not chunks:
            raise DocumentParseError("The document produced no chunks")

        provider = build_embedding_provider(settings)
        vectors = await provider.embed_documents([chunk.content for chunk in chunks])

    except DocumentParseError as exc:
        # A malformed document will not become readable on retry.
        await _record_failure(document_id, str(exc))
        logger.warning("knowledge_ingest_failed", reason="parse", error=str(exc))
        return {"result": "failed_parse", "document_id": str(document_id), "chunks": 0}

    except ProviderError as exc:
        if exc.retryable:
            # Left PROCESSING so the retry picks it up; the transport backs off.
            logger.warning("knowledge_ingest_retrying", error=str(exc))
            raise RetryableSendError(str(exc), retry_after_seconds=30.0) from exc

        await _record_failure(document_id, str(exc))
        logger.warning("knowledge_ingest_failed", reason="embedding", error=str(exc))
        return {"result": "failed_embedding", "document_id": str(document_id), "chunks": 0}

    async with factory() as session:
        document = await knowledge_repo.get_document(session, uuid.UUID(str(document_id)))
        if document is None:
            # Deleted while the embedding call was in flight. The vectors have
            # nowhere to go; dropping them is the convergent outcome.
            logger.info("knowledge_ingest_skipped", reason="document_deleted_mid_ingest")
            return {"result": "deleted_during_ingest", "document_id": str(document_id)}

        stored = await knowledge_repo.replace_chunks(
            session,
            document=document,
            chunks=chunks,
            vectors=vectors,
            embedding_model=provider.model_id,
        )
        await knowledge_repo.mark_ready(session, document, chunk_count=stored)

        job = await knowledge_repo.get_job_for_document(session, document.id)
        if job is not None:
            await knowledge_repo.complete_job(session, job, chunks_created=stored)
        await session.commit()

    logger.info("knowledge_ingest_completed", chunks=stored, format=parsed.format.value)
    return {"result": "ready", "document_id": str(document_id), "chunks": stored}


async def _record_failure(document_id: object, reason: str) -> None:
    """Persist a terminal ingestion failure on the document and its job."""
    factory = get_session_factory()
    async with factory() as session:
        document = await knowledge_repo.get_document(session, uuid.UUID(str(document_id)))
        if document is None:
            return
        await knowledge_repo.mark_failed(session, document, reason=reason)

        job = await knowledge_repo.get_job_for_document(session, document.id)
        if job is not None:
            await knowledge_repo.fail_job(session, job, reason=reason)
        await session.commit()
