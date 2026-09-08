"""Cross-product RAG isolation (Phase 21, §28.3, [D56]).

The invariant: **a conversation for one product is structurally incapable of retrieving
another product's knowledge.** Structurally, not by convention — `search` cannot be
called without a product and no longer accepts a knowledge-base ID at all, so a foreign
one cannot be supplied by accident or by a later refactor.

These tests are adversarial on purpose. Several give two products **identical embeddings
and identical text**, so similarity cannot possibly separate them and only the product
predicate can. A missing `WHERE` would show up here as a returned chunk rather than as a
subtly worse ranking, which is the failure that would otherwise go unnoticed.

Products and segments are created dynamically. Nothing depends on Academicae's slug,
name or UUID.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import DocumentStatus
from app.persistence.models import (
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
    Product,
    Segment,
)
from app.persistence.repositories import knowledge as knowledge_repo
from tests.integration.helpers import run_db

pytestmark = pytest.mark.integration

MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DIMENSION = 384

#: One vector, used for every chunk in most tests. Identical embeddings mean similarity
#: cannot distinguish anything: whatever comes back came back because of the filters.
IDENTICAL = [0.1] * DIMENSION


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    async def clear(session: AsyncSession) -> None:
        await session.execute(delete(KnowledgeChunk))
        await session.execute(delete(KnowledgeDocument))
        await session.execute(delete(KnowledgeBase))
        await session.execute(delete(Segment))
        await session.execute(delete(Product).where(Product.slug.like("r-%")))

    run_db(clear)
    yield
    run_db(clear)


def make_product(slug: str) -> uuid.UUID:
    async def create(session: AsyncSession) -> uuid.UUID:
        product = Product(slug=slug, display_name=slug.upper())
        session.add(product)
        await session.flush()
        return product.id

    return run_db(create)


def make_segment(product_id: uuid.UUID, slug: str) -> uuid.UUID:
    async def create(session: AsyncSession) -> uuid.UUID:
        segment = Segment(product_id=product_id, slug=slug, display_name=slug.title())
        session.add(segment)
        await session.flush()
        return segment.id

    return run_db(create)


def make_knowledge(
    *,
    product_id: uuid.UUID | None,
    segment_id: uuid.UUID | None = None,
    name: str,
    content: str,
    status: DocumentStatus = DocumentStatus.READY,
    embedding: list[float] | None = None,
    embedding_model: str = MODEL,
) -> uuid.UUID:
    """A knowledge base with one document and one chunk."""

    async def create(session: AsyncSession) -> uuid.UUID:
        base = KnowledgeBase(product_id=product_id, segment_id=segment_id, name=name)
        session.add(base)
        await session.flush()

        document = KnowledgeDocument(
            knowledge_base_id=base.id, title=f"{name} document", status=status, chunk_count=1
        )
        session.add(document)
        await session.flush()

        session.add(
            KnowledgeChunk(
                document_id=document.id,
                chunk_index=0,
                content=content,
                embedding=embedding if embedding is not None else IDENTICAL,
                embedding_model=embedding_model,
            )
        )
        await session.flush()
        return base.id

    return run_db(create)


def search(
    product_id: uuid.UUID, *, segment_id: uuid.UUID | None = None, limit: int = 20
) -> list[str]:
    """Retrieved chunk contents, which is what an answer would be grounded in."""

    async def run(session: AsyncSession) -> list[str]:
        results = await knowledge_repo.search(
            session,
            product_id=product_id,
            segment_id=segment_id,
            query_vector=IDENTICAL,
            limit=limit,
            embedding_model=MODEL,
        )
        return [chunk.content for chunk in results]

    return run_db(run)


class TestCrossProductIsolation:
    def test_identical_content_in_two_products_stays_separate(self) -> None:
        """The strongest form of the test.

        Both products hold the *same sentence* with the *same vector*. Similarity cannot
        tell them apart, so anything crossing the boundary would be the filter's fault
        and nothing else's.
        """
        alpha = make_product("r-alpha")
        beta = make_product("r-beta")

        shared_text = "Our platform onboards providers in under a week."
        make_knowledge(product_id=alpha, name="alpha-kb", content=shared_text)
        make_knowledge(product_id=beta, name="beta-kb", content=shared_text)

        assert search(alpha) == [shared_text]
        assert search(beta) == [shared_text]

        # One row each, not two: neither saw the other's copy.
        assert len(search(alpha)) == 1
        assert len(search(beta)) == 1

    def test_a_product_never_sees_another_products_distinct_content(self) -> None:
        alpha = make_product("r-alpha")
        beta = make_product("r-beta")

        make_knowledge(product_id=alpha, name="alpha-kb", content="Alpha sells telescopes.")
        make_knowledge(product_id=beta, name="beta-kb", content="Beta sells bicycles.")

        assert search(alpha) == ["Alpha sells telescopes."]
        assert search(beta) == ["Beta sells bicycles."]

    def test_a_segment_slug_shared_between_products_changes_nothing(self) -> None:
        """Two products may each sell to government. The product boundary wins."""
        alpha = make_product("r-alpha")
        beta = make_product("r-beta")
        alpha_gov = make_segment(alpha, "government")
        beta_gov = make_segment(beta, "government")

        make_knowledge(
            product_id=alpha,
            segment_id=alpha_gov,
            name="alpha-gov",
            content="Alpha for government.",
        )
        make_knowledge(
            product_id=beta, segment_id=beta_gov, name="beta-gov", content="Beta for government."
        )

        assert search(alpha, segment_id=alpha_gov) == ["Alpha for government."]
        assert search(beta, segment_id=beta_gov) == ["Beta for government."]

    def test_a_foreign_segment_id_retrieves_nothing_foreign(self) -> None:
        """Passing another product's segment must not reach into that product.

        The nearest thing left to a "foreign ID attack" now that a knowledge-base ID
        cannot be passed at all: the product predicate still stands in the way.
        """
        alpha = make_product("r-alpha")
        beta = make_product("r-beta")
        beta_segment = make_segment(beta, "industry")

        make_knowledge(product_id=alpha, name="alpha-shared", content="Alpha shared knowledge.")
        make_knowledge(
            product_id=beta,
            segment_id=beta_segment,
            name="beta-seg",
            content="Beta segment secret.",
        )

        results = search(alpha, segment_id=beta_segment)
        assert "Beta segment secret." not in results
        assert results == ["Alpha shared knowledge."]


class TestSharedAndSegmentKnowledge:
    def test_shared_knowledge_is_visible_to_a_segment(self) -> None:
        """The Life Herba requirement: a pharmacist asking how consumers buy medicines
        must be answered from the shared ecosystem knowledge."""
        product = make_product("r-herba")
        pharmacies = make_segment(product, "pharmacies")

        make_knowledge(
            product_id=product,
            name="ecosystem",
            content="Consumers purchase medicines through the app.",
        )

        assert search(product, segment_id=pharmacies) == [
            "Consumers purchase medicines through the app."
        ]

    def test_a_segment_sees_shared_plus_its_own_and_not_another_segments(self) -> None:
        product = make_product("r-herba")
        pharmacies = make_segment(product, "pharmacies")
        consumers = make_segment(product, "consumers")

        make_knowledge(product_id=product, name="shared", content="Shared ecosystem facts.")
        make_knowledge(
            product_id=product, segment_id=pharmacies, name="pharm", content="Pharmacy onboarding."
        )
        make_knowledge(
            product_id=product, segment_id=consumers, name="cons", content="Consumer-only pricing."
        )

        results = set(search(product, segment_id=pharmacies))
        assert results == {"Shared ecosystem facts.", "Pharmacy onboarding."}
        assert "Consumer-only pricing." not in results

    def test_no_segment_context_sees_shared_knowledge_only(self) -> None:
        """Unknown segment must **narrow**, not widen.

        If a NULL segment matched every segment-restricted base, missing context would
        grant more access than known context — the opposite of failing closed.
        """
        product = make_product("r-herba")
        pharmacies = make_segment(product, "pharmacies")

        make_knowledge(product_id=product, name="shared", content="Shared ecosystem facts.")
        make_knowledge(
            product_id=product, segment_id=pharmacies, name="pharm", content="Pharmacy onboarding."
        )

        assert search(product, segment_id=None) == ["Shared ecosystem facts."]

    def test_a_product_with_only_shared_knowledge_works_for_every_segment(self) -> None:
        """The SustainX shape: several marketing segments, one shared knowledge base,
        no segment-specific bases required and no special-casing anywhere."""
        product = make_product("r-sustain")
        government = make_segment(product, "government")
        industry = make_segment(product, "industry")

        make_knowledge(product_id=product, name="shared", content="The four core services.")

        assert search(product, segment_id=government) == ["The four core services."]
        assert search(product, segment_id=industry) == ["The four core services."]
        assert search(product, segment_id=None) == ["The four core services."]


class TestIneligibleKnowledge:
    def test_an_unattributed_knowledge_base_is_never_retrieved(self) -> None:
        """During the Phase 18→26 transition `product_id` is nullable, and NULL means
        **not safe to retrieve** — never *shared with everyone*.

        Getting this backwards would turn a migration gap into a knowledge leak.
        """
        product = make_product("r-alpha")
        make_knowledge(product_id=product, name="owned", content="Owned knowledge.")
        make_knowledge(product_id=None, name="orphan", content="Unattributed knowledge.")

        assert search(product) == ["Owned knowledge."]

    def test_a_product_with_no_knowledge_retrieves_nothing(self) -> None:
        """Never another product's. Fail closed."""
        alpha = make_product("r-alpha")
        beta = make_product("r-beta")
        make_knowledge(product_id=beta, name="beta-kb", content="Beta knowledge.")

        assert search(alpha) == []

    def test_documents_that_are_not_ready_are_excluded(self) -> None:
        product = make_product("r-alpha")
        make_knowledge(
            product_id=product,
            name="pending",
            content="Half-ingested.",
            status=DocumentStatus.PENDING,
        )
        make_knowledge(
            product_id=product,
            name="archived",
            content="Withdrawn.",
            status=DocumentStatus.ARCHIVED,
        )
        make_knowledge(product_id=product, name="ready", content="Current.")

        assert search(product) == ["Current."]

    def test_a_different_embedding_model_is_excluded(self) -> None:
        """[D1]: cosine distance between vectors from different models is arithmetically
        valid and semantically meaningless."""
        product = make_product("r-alpha")
        make_knowledge(
            product_id=product,
            name="old",
            content="Embedded by an older model.",
            embedding_model="some-other-model",
        )
        make_knowledge(product_id=product, name="current", content="Current model.")

        assert search(product) == ["Current model."]


class TestSeveralKnowledgeBasesInOneProduct:
    def test_all_of_a_products_shared_bases_are_searched(self) -> None:
        """No arbitrary "first base" is chosen.

        The removed `_knowledge_base_id` picked one; a product may legitimately own
        several, and every eligible one contributes.
        """
        product = make_product("r-alpha")
        make_knowledge(product_id=product, name="policies", content="Refunds within 30 days.")
        make_knowledge(product_id=product, name="services", content="We offer four services.")

        assert set(search(product)) == {"Refunds within 30 days.", "We offer four services."}
