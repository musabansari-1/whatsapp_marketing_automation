"""Ingestion pipeline and pgvector retrieval tests (§11, §17, §20, [D1]).

These need a real database: pgvector similarity, the READY-only filter and the
embedding-model filter cannot be verified against SQLAlchemy metadata.

Embedding goes through `FakeEmbeddingProvider`, so no model is downloaded and the
vectors are deterministic.
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NonRetryableProviderError, RetryableProviderError
from app.domain.enums import DocumentStatus, IngestionJobStatus
from app.integrations.embedding import FakeEmbeddingProvider
from app.integrations.queue import TaskRequest, TaskType
from app.persistence.models import (
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIngestionJob,
)
from app.persistence.repositories import knowledge as knowledge_repo
from app.worker.handlers.campaign_send import RetryableSendError
from app.worker.handlers.knowledge_ingest import ingest_knowledge_document
from tests.integration.helpers import ensure_product_id, get_test_database_url, run_db

pytestmark = pytest.mark.integration

POLICY = b"Our refund window is thirty days from the date of purchase."


@pytest.fixture
def ingest_env(engine: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeEmbeddingProvider]:
    """A clean knowledge schema and a fake embedding provider the handler will use."""
    from app.core.config import reset_settings_cache
    from app.persistence import database

    monkeypatch.setenv("DATABASE_URL", get_test_database_url())
    reset_settings_cache()
    database._engine = None  # noqa: SLF001
    database._session_factory = None  # noqa: SLF001

    provider = FakeEmbeddingProvider(dimension=384)
    monkeypatch.setattr(
        "app.worker.handlers.knowledge_ingest.build_embedding_provider",
        lambda _settings: provider,
    )

    async def clear(session: AsyncSession) -> None:
        await session.execute(delete(KnowledgeChunk))
        await session.execute(delete(KnowledgeIngestionJob))
        await session.execute(delete(KnowledgeDocument))
        await session.execute(delete(KnowledgeBase))

    run_db(clear)
    yield provider
    run_db(clear)


def seed_document(*, filename: str = "refunds.txt", content: bytes = POLICY) -> tuple[str, str]:
    """Create a knowledge base and a PENDING document; return both IDs."""

    async def seed(session: AsyncSession) -> tuple[str, str]:
        base = await knowledge_repo.create_knowledge_base(
            session,
            name=f"KB {uuid.uuid4()}",
            description=None,
            # Retrieval is product-scoped since Phase 21 ([D56]); an unattributed base
            # is deliberately not retrievable, so the fixture must own one.
            product_id=await ensure_product_id(session),
        )
        document = await knowledge_repo.create_document(
            session,
            knowledge_base_id=base.id,
            title=filename,
            source_filename=filename,
            content_type="text/plain",
            byte_size=len(content),
            digest=knowledge_repo.content_hash(content),
        )
        await knowledge_repo.create_job(
            session, document_id=document.id, task_id=f"ingest-{document.id}"
        )
        return str(base.id), str(document.id)

    return run_db(seed)


def run_ingest(document_id: str, content: bytes = POLICY) -> dict[str, Any]:
    """Run the handler on its own event loop, as the worker would."""
    from app.persistence import database

    async def run() -> dict[str, Any]:
        database._engine = None  # noqa: SLF001
        database._session_factory = None  # noqa: SLF001
        try:
            result = await ingest_knowledge_document(
                TaskRequest(
                    task_type=TaskType.INGEST_KNOWLEDGE_DOCUMENT,
                    task_id=f"ingest-{document_id}",
                    payload={
                        "document_id": document_id,
                        "content": base64.b64encode(content).decode(),
                    },
                )
            )
            return dict(result)
        finally:
            await database.dispose_engine()

    return asyncio.run(run())


def fetch_document(document_id: str) -> KnowledgeDocument:
    async def fetch(session: AsyncSession) -> KnowledgeDocument:
        document = await knowledge_repo.get_document(session, uuid.UUID(document_id))
        assert document is not None
        return document

    return run_db(fetch)


def fetch_job(document_id: str) -> KnowledgeIngestionJob:
    async def fetch(session: AsyncSession) -> KnowledgeIngestionJob:
        job = await knowledge_repo.get_job_for_document(session, uuid.UUID(document_id))
        assert job is not None
        return job

    return run_db(fetch)


def chunk_count(document_id: str) -> int:
    async def count(session: AsyncSession) -> int:
        return await knowledge_repo.count_chunks(session, uuid.UUID(document_id))

    return run_db(count)


class TestIngestionPipeline:
    def test_a_document_becomes_ready_with_chunks(self, ingest_env: FakeEmbeddingProvider) -> None:
        _base_id, document_id = seed_document()

        result = run_ingest(document_id)

        assert result["result"] == "ready"
        assert result["chunks"] >= 1
        assert fetch_document(document_id).status is DocumentStatus.READY
        assert chunk_count(document_id) == result["chunks"]
        assert fetch_job(document_id).status is IngestionJobStatus.COMPLETED

    def test_the_producing_model_is_recorded_on_each_chunk(
        self, ingest_env: FakeEmbeddingProvider
    ) -> None:
        """[D1]: without this, a search after a model change would compare vectors
        that are not comparable."""
        _base_id, document_id = seed_document()
        run_ingest(document_id)

        async def models(session: AsyncSession) -> set[str]:
            from sqlalchemy import select

            result = await session.execute(
                select(KnowledgeChunk.embedding_model).where(
                    KnowledgeChunk.document_id == uuid.UUID(document_id)
                )
            )
            return {model for model in result.scalars().all() if model is not None}

        assert run_db(models) == {ingest_env.model_id}

    def test_re_ingestion_replaces_rather_than_duplicates(
        self, ingest_env: FakeEmbeddingProvider
    ) -> None:
        """§20 idempotency: a redelivered task must converge, not double every chunk
        and skew retrieval toward whatever arrived twice."""
        _base_id, document_id = seed_document()

        first = run_ingest(document_id)
        second = run_ingest(document_id)

        assert first["chunks"] == second["chunks"]
        assert chunk_count(document_id) == first["chunks"]

    def test_an_unparseable_document_fails_visibly(self, ingest_env: FakeEmbeddingProvider) -> None:
        """§17: the uploader must be able to see why it failed, so the reason is
        stored rather than only logged."""
        _base_id, document_id = seed_document(filename="broken.pdf", content=b"not a pdf")

        result = run_ingest(document_id, content=b"not a pdf")

        assert result["result"] == "failed_parse"
        document = fetch_document(document_id)
        assert document.status is DocumentStatus.FAILED
        assert document.error_message
        assert fetch_job(document_id).status is IngestionJobStatus.FAILED

    def test_a_parse_failure_settles_instead_of_retrying(
        self, ingest_env: FakeEmbeddingProvider
    ) -> None:
        """A malformed document will not become readable on the fifth attempt.
        Returning rather than raising is what makes the worker answer 200."""
        _base_id, document_id = seed_document(filename="empty.txt", content=b"   \n  ")

        assert run_ingest(document_id, content=b"   \n  ")["result"] == "failed_parse"

    def test_a_transient_embedding_failure_is_retried(
        self, ingest_env: FakeEmbeddingProvider
    ) -> None:
        """Raising RetryableSendError is what makes the worker answer 503 with a
        Retry-After — the retry protocol lives in the HTTP status."""
        _base_id, document_id = seed_document()
        ingest_env.fail_with = RetryableProviderError("temporarily unavailable", provider="fake")

        with pytest.raises(RetryableSendError):
            run_ingest(document_id)

        # PROCESSING, not FAILED: the retry will finish the job.
        assert fetch_document(document_id).status is DocumentStatus.PROCESSING

    def test_a_retry_after_a_transient_failure_succeeds(
        self, ingest_env: FakeEmbeddingProvider
    ) -> None:
        _base_id, document_id = seed_document()
        ingest_env.fail_with = RetryableProviderError("temporarily unavailable", provider="fake")

        with pytest.raises(RetryableSendError):
            run_ingest(document_id)

        ingest_env.fail_with = None
        assert run_ingest(document_id)["result"] == "ready"
        assert fetch_document(document_id).status is DocumentStatus.READY

    def test_a_permanent_embedding_failure_is_recorded(
        self, ingest_env: FakeEmbeddingProvider
    ) -> None:
        _base_id, document_id = seed_document()
        ingest_env.fail_with = NonRetryableProviderError("dimension mismatch", provider="fake")

        assert run_ingest(document_id)["result"] == "failed_embedding"
        assert fetch_document(document_id).status is DocumentStatus.FAILED

    def test_a_missing_document_id_is_rejected(self, ingest_env: FakeEmbeddingProvider) -> None:
        async def run() -> None:
            await ingest_knowledge_document(
                TaskRequest(
                    task_type=TaskType.INGEST_KNOWLEDGE_DOCUMENT,
                    task_id="ingest-none",
                    payload={},
                )
            )

        with pytest.raises(ValueError, match="document_id"):
            asyncio.run(run())

    def test_missing_content_is_rejected(self, ingest_env: FakeEmbeddingProvider) -> None:
        async def run() -> None:
            await ingest_knowledge_document(
                TaskRequest(
                    task_type=TaskType.INGEST_KNOWLEDGE_DOCUMENT,
                    task_id="ingest-none",
                    payload={"document_id": str(uuid.uuid4())},
                )
            )

        with pytest.raises(ValueError, match="content"):
            asyncio.run(run())


class TestVectorRetrieval:
    """pgvector search behaviour that only a real database can demonstrate."""

    def test_an_ingested_document_is_retrievable(self, ingest_env: FakeEmbeddingProvider) -> None:
        base_id, document_id = seed_document()
        run_ingest(document_id)

        async def search(session: AsyncSession) -> list[Any]:
            vector = await ingest_env.embed_query("refund policy")
            return await knowledge_repo.search(
                session,
                product_id=await ensure_product_id(session),
                query_vector=vector,
                limit=5,
                embedding_model=ingest_env.model_id,
            )

        results = run_db(search)
        assert results
        assert all(0.0 <= chunk.similarity <= 1.0 for chunk in results)
        assert results[0].document_title == "refunds.txt"

    def test_results_are_ordered_by_similarity(self, ingest_env: FakeEmbeddingProvider) -> None:
        base_id, document_id = seed_document(
            content=b"Refunds are processed within thirty days.\n\n"
            b"Our office is located in Mumbai near the station.\n\n"
            b"Support hours are nine to six on weekdays."
        )
        run_ingest(
            document_id,
            content=b"Refunds are processed within thirty days.\n\n"
            b"Our office is located in Mumbai near the station.\n\n"
            b"Support hours are nine to six on weekdays.",
        )

        async def search(session: AsyncSession) -> list[Any]:
            vector = await ingest_env.embed_query("refunds")
            return await knowledge_repo.search(
                session,
                product_id=await ensure_product_id(session),
                query_vector=vector,
                limit=10,
                embedding_model=ingest_env.model_id,
            )

        results = run_db(search)
        similarities = [chunk.similarity for chunk in results]
        assert similarities == sorted(similarities, reverse=True)

    def test_a_document_that_is_not_ready_is_not_retrievable(
        self, ingest_env: FakeEmbeddingProvider
    ) -> None:
        """§11 restricts retrieval to READY documents; a half-ingested one would
        otherwise contribute partial content to answers."""
        base_id, document_id = seed_document()
        run_ingest(document_id)

        async def unready(session: AsyncSession) -> None:
            document = await knowledge_repo.get_document(session, uuid.UUID(document_id))
            assert document is not None
            document.status = DocumentStatus.PROCESSING

        run_db(unready)

        async def search(session: AsyncSession) -> list[Any]:
            vector = await ingest_env.embed_query("refund policy")
            return await knowledge_repo.search(
                session,
                product_id=await ensure_product_id(session),
                query_vector=vector,
                limit=5,
            )

        assert run_db(search) == []

    def test_an_archived_document_is_not_retrievable(
        self, ingest_env: FakeEmbeddingProvider
    ) -> None:
        """Archiving takes content out of circulation without destroying the chunks
        that answers already cite."""
        base_id, document_id = seed_document()
        run_ingest(document_id)

        async def archive(session: AsyncSession) -> None:
            document = await knowledge_repo.get_document(session, uuid.UUID(document_id))
            assert document is not None
            await knowledge_repo.archive_document(session, document)

        run_db(archive)

        async def search(session: AsyncSession) -> list[Any]:
            vector = await ingest_env.embed_query("refund policy")
            return await knowledge_repo.search(
                session,
                product_id=await ensure_product_id(session),
                query_vector=vector,
                limit=5,
            )

        assert run_db(search) == []
        # The chunks survive, so citations in ai_runs stay resolvable.
        assert chunk_count(document_id) > 0

    def test_vectors_from_another_model_are_excluded(
        self, ingest_env: FakeEmbeddingProvider
    ) -> None:
        """[D1]: cosine distance between two models' vectors is arithmetically valid
        and semantically meaningless, so an unfiltered search returns confident
        nonsense."""
        base_id, document_id = seed_document()
        run_ingest(document_id)

        async def search(session: AsyncSession) -> list[Any]:
            vector = await ingest_env.embed_query("refund policy")
            return await knowledge_repo.search(
                session,
                product_id=await ensure_product_id(session),
                query_vector=vector,
                limit=5,
                embedding_model="some-other-model",
            )

        assert run_db(search) == []

    def test_another_products_knowledge_is_not_searched(
        self, ingest_env: FakeEmbeddingProvider
    ) -> None:
        _base_id, document_id = seed_document()
        run_ingest(document_id)

        async def search(session: AsyncSession) -> list[Any]:
            vector = await ingest_env.embed_query("refund policy")
            return await knowledge_repo.search(
                session,
                product_id=uuid.uuid4(),
                query_vector=vector,
                limit=5,
            )

        assert run_db(search) == []

    def test_the_limit_is_respected(self, ingest_env: FakeEmbeddingProvider) -> None:
        paragraphs = b"\n\n".join(
            f"Paragraph {n} discusses a distinct aspect of our refund policy in detail.".encode()
            for n in range(8)
        )
        base_id, document_id = seed_document(content=paragraphs)
        run_ingest(document_id, content=paragraphs)

        async def search(session: AsyncSession) -> list[Any]:
            vector = await ingest_env.embed_query("refund")
            return await knowledge_repo.search(
                session,
                product_id=await ensure_product_id(session),
                query_vector=vector,
                limit=2,
            )

        assert len(run_db(search)) <= 2
