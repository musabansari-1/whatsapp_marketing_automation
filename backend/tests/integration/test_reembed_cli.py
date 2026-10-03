"""Re-embed command tests ([D1] model upgrade path).

After the configured embedding model changes, stored vectors belong to the old
space and retrieval filters them out — so chunks must be re-encoded. The command
brings every row onto the configured model; rows already there are skipped, so a
re-run (or an interrupted run resumed) converges instead of redoing work.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.cli import EXIT_OK
from app.core.config import reset_settings_cache
from app.domain.enums import DocumentStatus
from app.integrations.embedding.fake import FAKE_EMBEDDING_MODEL, FakeEmbeddingProvider
from app.persistence import database
from app.persistence.models import KnowledgeBase, KnowledgeChunk, KnowledgeDocument, Product
from tests.integration.helpers import get_test_database_url, run_db

pytestmark = pytest.mark.integration

OLD_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


@pytest_asyncio.fixture
async def db(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def clear() -> None:
        async with factory() as session:
            await session.execute(delete(KnowledgeChunk))
            await session.execute(delete(KnowledgeDocument))
            await session.execute(delete(KnowledgeBase))
            await session.execute(delete(Product).where(Product.slug.like("e-%")))
            await session.commit()

    await clear()
    yield factory
    await clear()


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DATABASE_URL", get_test_database_url())
    reset_settings_cache()
    database._engine = None  # noqa: SLF001
    database._session_factory = None  # noqa: SLF001
    yield
    database._engine = None  # noqa: SLF001
    database._session_factory = None  # noqa: SLF001
    reset_settings_cache()


async def seed(session: AsyncSession, *, old: int, current: int) -> None:
    provider = FakeEmbeddingProvider(dimension=384)
    product = Product(slug=f"e-{uuid.uuid4().hex[:8]}", display_name="Embed")
    session.add(product)
    await session.flush()
    base = KnowledgeBase(product_id=product.id, name="KB")
    session.add(base)
    await session.flush()
    document = KnowledgeDocument(
        knowledge_base_id=base.id, title="doc", status=DocumentStatus.READY, chunk_count=0
    )
    session.add(document)
    await session.flush()
    for index in range(old):
        vector = (await provider.embed_documents([f"old chunk {index}"]))[0]
        session.add(
            KnowledgeChunk(
                document_id=document.id,
                chunk_index=index,
                content=f"old chunk {index}",
                embedding=list(vector),
                embedding_model=OLD_MODEL,
            )
        )
    for index in range(current):
        vector = (await provider.embed_documents([f"new chunk {index}"]))[0]
        session.add(
            KnowledgeChunk(
                document_id=document.id,
                chunk_index=100 + index,
                content=f"new chunk {index}",
                embedding=list(vector),
                embedding_model=FAKE_EMBEDDING_MODEL,
            )
        )
    await session.commit()


def chunk_models() -> list[str | None]:
    async def fetch(session: AsyncSession) -> list[str | None]:
        rows = await session.execute(
            select(KnowledgeChunk.embedding_model).order_by(KnowledgeChunk.chunk_index)
        )
        return list(rows.scalars().all())

    return run_db(fetch)


def chunk_contents() -> list[str]:
    async def fetch(session: AsyncSession) -> list[str]:
        rows = await session.execute(
            select(KnowledgeChunk.content).order_by(KnowledgeChunk.chunk_index)
        )
        return list(rows.scalars().all())

    return run_db(fetch)


def re_embed_sync(capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    """Run the real CLI entrypoint: it disposes the engine on exit, so repeated
    runs in one test get a fresh engine on the new loop instead of sharing one
    across loops."""
    from app.cli import main

    code = main(["re-embed-knowledge"])
    return code, capsys.readouterr().out


class TestReEmbedKnowledge:
    def test_old_vectors_move_to_the_configured_model(
        self,
        db: async_sessionmaker[AsyncSession],
        cli_env: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        run_db(lambda session: seed(session, old=3, current=1))

        code, out = re_embed_sync(capsys)

        assert code == EXIT_OK
        assert "3 chunks" in out
        assert chunk_models() == [FAKE_EMBEDDING_MODEL] * 4

    def test_a_second_run_changes_nothing(
        self,
        db: async_sessionmaker[AsyncSession],
        cli_env: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        run_db(lambda session: seed(session, old=2, current=0))
        assert re_embed_sync(capsys)[0] == EXIT_OK

        code, out = re_embed_sync(capsys)

        assert code == EXIT_OK
        assert "0 chunks" in out

    def test_content_is_untouched(
        self,
        db: async_sessionmaker[AsyncSession],
        cli_env: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        run_db(lambda session: seed(session, old=1, current=0))
        assert re_embed_sync(capsys)[0] == EXIT_OK

        assert chunk_contents() == ["old chunk 0"]
