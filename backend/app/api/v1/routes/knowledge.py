"""Knowledge base endpoints, per product (specification §11, §16, §17, §19, §28).

Upload records the document and queues ingestion; parsing and embedding happen in the
worker, because §4 forbids slow work in a request and embedding a large document takes
seconds.

Every route is inside one product (Phase 26), so a knowledge base is always created with
an owner: `knowledge_bases.product_id` is NOT NULL, and what one product's assistant
knows cannot be read, extended or archived from another product's address. A base with no
segment is shared across that product's segments ([D56]); a segment, when given, must be
one of this product's.

Knowledge is configuration, so an inactive product may still build it. That is how a
product is prepared before it goes live.

There is no retrieval endpoint. Retrieval is something the AI does inside a conversation,
and exposing it here would invite use from places §9 does not permit.
"""

from __future__ import annotations

import base64
import uuid

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status

from app.api.deps import CurrentUser, DbSession, Queue, SettingsDep
from app.api.rate_limit import enforce
from app.api.schemas import (
    CreateKnowledgeBaseRequest,
    KnowledgeBaseResponse,
    KnowledgeDocumentResponse,
)
from app.api.scope import PRODUCT_PREFIX, ProductScope, owned_by, segment_for_write
from app.core.logging import get_logger
from app.domain.documents import SUPPORTED_EXTENSIONS, UnsupportedFormatError, format_for_filename
from app.domain.enums import DocumentStatus
from app.domain.rate_limit import RateLimitScope
from app.integrations.queue import TaskRequest, TaskType
from app.persistence.models import KnowledgeBase, KnowledgeDocument, Product
from app.persistence.repositories import knowledge as knowledge_repo

router = APIRouter(prefix=f"{PRODUCT_PREFIX}/knowledge-bases", tags=["knowledge"])
logger = get_logger(__name__)


@router.post("", response_model=KnowledgeBaseResponse, status_code=status.HTTP_201_CREATED)
async def create_knowledge_base(
    payload: CreateKnowledgeBaseRequest,
    user: CurrentUser,
    product: ProductScope,
    session: DbSession,
) -> KnowledgeBaseResponse:
    """Create a knowledge base owned by this product.

    Names are unique within a product, compared case-insensitively. Two products may each
    have a "Product FAQs".
    """
    segment_id = await segment_for_write(session, product, payload.segment_id)

    if await knowledge_repo.find_by_name(session, product_id=product.id, name=payload.name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A knowledge base with that name already exists in this product",
        )

    knowledge_base = await knowledge_repo.create_knowledge_base(
        session,
        name=payload.name,
        description=payload.description,
        product_id=product.id,
        segment_id=segment_id,
    )
    logger.info(
        "knowledge_base_created",
        knowledge_base_id=str(knowledge_base.id),
        product_id=str(product.id),
        by_user_id=str(user.id),
    )
    return KnowledgeBaseResponse.model_validate(knowledge_base)


@router.get("", response_model=list[KnowledgeBaseResponse])
async def list_knowledge_bases(
    product: ProductScope, session: DbSession
) -> list[KnowledgeBaseResponse]:
    bases = await knowledge_repo.list_knowledge_bases(session, product_id=product.id)
    return [KnowledgeBaseResponse.model_validate(base) for base in bases]


@router.get("/{knowledge_base_id}", response_model=KnowledgeBaseResponse)
async def get_knowledge_base(
    knowledge_base_id: uuid.UUID, product: ProductScope, session: DbSession
) -> KnowledgeBaseResponse:
    return KnowledgeBaseResponse.model_validate(
        await _require_knowledge_base(session, product, knowledge_base_id)
    )


@router.post(
    "/{knowledge_base_id}/documents",
    response_model=KnowledgeDocumentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    knowledge_base_id: uuid.UUID,
    user: CurrentUser,
    product: ProductScope,
    session: DbSession,
    settings: SettingsDep,
    queue: Queue,
    file: UploadFile = File(...),
) -> KnowledgeDocumentResponse:
    """Accept a document into one of this product's knowledge bases and queue ingestion.

    Returns immediately with the document PENDING. Only a READY document is
    retrievable (§11), so the dashboard shows progress rather than pretending the
    upload is usable straight away.
    """
    # §19: uploads are authenticated, so the exposure is lower than the webhook's — but
    # each one costs parsing and embedding work in the worker, and nothing else bounds
    # how many a single user can start.
    await enforce(
        session,
        settings,
        scope=RateLimitScope.UPLOAD_USER,
        identifier=str(user.id),
    )

    knowledge_base = await _require_knowledge_base(session, product, knowledge_base_id)

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
    product: ProductScope,
    session: DbSession,
    include_archived: bool = Query(default=False),
) -> list[KnowledgeDocumentResponse]:
    await _require_knowledge_base(session, product, knowledge_base_id)
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
    product: ProductScope,
    session: DbSession,
) -> KnowledgeDocumentResponse:
    document = await _require_document(session, product, knowledge_base_id, document_id)
    return KnowledgeDocumentResponse.model_validate(document)


@router.post(
    "/{knowledge_base_id}/documents/{document_id}/archive",
    response_model=KnowledgeDocumentResponse,
)
async def archive_document(
    knowledge_base_id: uuid.UUID,
    document_id: uuid.UUID,
    product: ProductScope,
    session: DbSession,
) -> KnowledgeDocumentResponse:
    """Take a document out of circulation (§17).

    Idempotent, and non-destructive: the chunks stay so that answers already given
    keep their citations, while retrieval stops returning them.
    """
    document = await _require_document(session, product, knowledge_base_id, document_id)

    if document.status is not DocumentStatus.ARCHIVED:
        await knowledge_repo.archive_document(session, document)
        logger.info("knowledge_document_archived", document_id=str(document.id))

    return KnowledgeDocumentResponse.model_validate(document)


async def _require_knowledge_base(
    session: DbSession, product: Product, knowledge_base_id: uuid.UUID
) -> KnowledgeBase:
    return owned_by(
        await knowledge_repo.get_knowledge_base(session, knowledge_base_id),
        product,
        not_found="Knowledge base not found",
    )


async def _require_document(
    session: DbSession, product: Product, knowledge_base_id: uuid.UUID, document_id: uuid.UUID
) -> KnowledgeDocument:
    await _require_knowledge_base(session, product, knowledge_base_id)
    document = await knowledge_repo.get_document(session, document_id)
    if document is None or document.knowledge_base_id != knowledge_base_id:
        # Checked rather than assumed: a document ID from another knowledge base must
        # not be readable by guessing the pair.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return document


__all__ = ["SUPPORTED_EXTENSIONS", "router"]
