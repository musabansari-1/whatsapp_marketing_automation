"""Knowledge base endpoints (specification §11, §16, §17, §19).

Upload records the document and queues ingestion; parsing and embedding happen in the
worker, because §4 forbids slow work in a request and embedding a large document takes
seconds.

There is no retrieval endpoint. Retrieval is not a dashboard feature — it is something
the AI does through an authorized tool in Phase 10, and exposing it as an API now would
invite use from places §9 does not permit.
"""

from __future__ import annotations

import base64
import uuid

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status

from app.api.deps import CurrentUser, DbSession, Queue, SettingsDep
from app.api.schemas import (
    CreateKnowledgeBaseRequest,
    KnowledgeBaseResponse,
    KnowledgeDocumentResponse,
)
from app.core.logging import get_logger
from app.domain.documents import SUPPORTED_EXTENSIONS, UnsupportedFormatError, format_for_filename
from app.domain.enums import DocumentStatus
from app.integrations.queue import TaskRequest, TaskType
from app.persistence.models import KnowledgeBase, KnowledgeDocument
from app.persistence.repositories import knowledge as knowledge_repo

router = APIRouter(prefix="/knowledge-bases", tags=["knowledge"])
logger = get_logger(__name__)


@router.post("", response_model=KnowledgeBaseResponse, status_code=status.HTTP_201_CREATED)
async def create_knowledge_base(
    payload: CreateKnowledgeBaseRequest, user: CurrentUser, session: DbSession
) -> KnowledgeBaseResponse:
    existing = [kb.name.casefold() for kb in await knowledge_repo.list_knowledge_bases(session)]
    if payload.name.casefold() in existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A knowledge base with that name already exists",
        )

    knowledge_base = await knowledge_repo.create_knowledge_base(
        session, name=payload.name, description=payload.description
    )
    logger.info("knowledge_base_created", knowledge_base_id=str(knowledge_base.id))
    return KnowledgeBaseResponse.model_validate(knowledge_base)


@router.get("", response_model=list[KnowledgeBaseResponse])
async def list_knowledge_bases(
    user: CurrentUser, session: DbSession
) -> list[KnowledgeBaseResponse]:
    bases = await knowledge_repo.list_knowledge_bases(session)
    return [KnowledgeBaseResponse.model_validate(base) for base in bases]


@router.get("/{knowledge_base_id}", response_model=KnowledgeBaseResponse)
async def get_knowledge_base(
    knowledge_base_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> KnowledgeBaseResponse:
    return KnowledgeBaseResponse.model_validate(
        await _require_knowledge_base(session, knowledge_base_id)
    )


@router.post(
    "/{knowledge_base_id}/documents",
    response_model=KnowledgeDocumentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    knowledge_base_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    settings: SettingsDep,
    queue: Queue,
    file: UploadFile = File(...),
) -> KnowledgeDocumentResponse:
    """Accept a document and queue it for ingestion.

    Returns immediately with the document PENDING. Only a READY document is
    retrievable (§11), so the dashboard shows progress rather than pretending the
    upload is usable straight away.
    """
    knowledge_base = await _require_knowledge_base(session, knowledge_base_id)

    filename = file.filename or "untitled"
    try:
        format_for_filename(filename)
    except UnsupportedFormatError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc

    content = await file.read()
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The file is empty")
    if len(content) > settings.knowledge_max_file_bytes:
        # §19: limit uploaded files. Checked against the bytes actually read rather
        # than a client-supplied Content-Length.
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"File exceeds the maximum size of {settings.knowledge_max_file_bytes} bytes",
        )

    digest = knowledge_repo.content_hash(content)
    duplicate = await knowledge_repo.find_by_hash(
        session, knowledge_base_id=knowledge_base.id, digest=digest
    )
    if duplicate is not None:
        # Re-ingesting identical bytes would double every chunk in the retrieval pool
        # and skew results toward whatever happened to be uploaded twice.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This document has already been uploaded as {duplicate.title!r}",
        )

    document = await knowledge_repo.create_document(
        session,
        knowledge_base_id=knowledge_base.id,
        title=filename,
        source_filename=filename,
        content_type=file.content_type,
        byte_size=len(content),
        digest=digest,
    )

    task_id = f"ingest-{document.id}"
    await knowledge_repo.create_job(session, document_id=document.id, task_id=task_id)

    await queue.enqueue(
        TaskRequest(
            task_type=TaskType.INGEST_KNOWLEDGE_DOCUMENT,
            # Deterministic, so a redelivered task collapses rather than re-ingesting.
            task_id=task_id,
            payload={
                "document_id": str(document.id),
                # V1 has no blob store, so the bytes travel with the task. Recorded as
                # a limitation in PROJECT_STATUS rather than hidden.
                "content": base64.b64encode(content).decode(),
            },
        )
    )

    logger.info(
        "knowledge_document_uploaded",
        document_id=str(document.id),
        bytes=len(content),
    )
    return KnowledgeDocumentResponse.model_validate(document)


@router.get("/{knowledge_base_id}/documents", response_model=list[KnowledgeDocumentResponse])
async def list_documents(
    knowledge_base_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    include_archived: bool = Query(default=False),
) -> list[KnowledgeDocumentResponse]:
    await _require_knowledge_base(session, knowledge_base_id)
    documents = await knowledge_repo.list_documents(
        session, knowledge_base_id, include_archived=include_archived
    )
    return [KnowledgeDocumentResponse.model_validate(document) for document in documents]


@router.get(
    "/{knowledge_base_id}/documents/{document_id}", response_model=KnowledgeDocumentResponse
)
async def get_document(
    knowledge_base_id: uuid.UUID,
    document_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
) -> KnowledgeDocumentResponse:
    document = await _require_document(session, knowledge_base_id, document_id)
    return KnowledgeDocumentResponse.model_validate(document)


@router.post(
    "/{knowledge_base_id}/documents/{document_id}/archive",
    response_model=KnowledgeDocumentResponse,
)
async def archive_document(
    knowledge_base_id: uuid.UUID,
    document_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
) -> KnowledgeDocumentResponse:
    """Take a document out of circulation (§17).

    Idempotent, and non-destructive: the chunks stay so that answers already given
    keep their citations, while retrieval stops returning them.
    """
    document = await _require_document(session, knowledge_base_id, document_id)

    if document.status is not DocumentStatus.ARCHIVED:
        await knowledge_repo.archive_document(session, document)
        logger.info("knowledge_document_archived", document_id=str(document.id))

    return KnowledgeDocumentResponse.model_validate(document)


async def _require_knowledge_base(
    session: DbSession, knowledge_base_id: uuid.UUID
) -> KnowledgeBase:
    knowledge_base = await knowledge_repo.get_knowledge_base(session, knowledge_base_id)
    if knowledge_base is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Knowledge base not found"
        )
    return knowledge_base


async def _require_document(
    session: DbSession, knowledge_base_id: uuid.UUID, document_id: uuid.UUID
) -> KnowledgeDocument:
    await _require_knowledge_base(session, knowledge_base_id)
    document = await knowledge_repo.get_document(session, document_id)
    if document is None or document.knowledge_base_id != knowledge_base_id:
        # Checked rather than assumed: a document ID from another knowledge base must
        # not be readable by guessing the pair.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return document


__all__ = ["SUPPORTED_EXTENSIONS", "router"]
