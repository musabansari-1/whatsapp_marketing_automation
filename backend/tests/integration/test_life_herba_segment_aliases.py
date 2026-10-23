"""Life Herba obsolete segment remapping (canonical launch slugs).

After configure-products (and the matching Alembic revision), Life Herba must not keep
both the Phase-27 seed slugs and the launch slugs. References move first; other products
are untouched.
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.cli import EXIT_OK, configure_products
from app.configuration.segment_aliases import (
    LIFE_HERBA_SEGMENT_RENAMES,
    reconcile_life_herba_segment_aliases,
)
from app.core.config import Settings, reset_settings_cache
from app.domain.enums import ConversationStatus, LeadStatus
from app.persistence import database
from app.persistence.models import AIConfiguration, Conversation, Lead, Product, Segment
from tests.integration.helpers import get_test_database_url

pytestmark = pytest.mark.integration

SHIPPED = Path(__file__).resolve().parents[2] / "config" / "products.json"
SLUGS = ("life-herba", "sustainx", "other-product", "academicae")


@pytest_asyncio.fixture
async def db(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def clear() -> None:
        async with factory() as session:
            ids = select(Product.id).where(Product.slug.in_(SLUGS))
            await session.execute(delete(Conversation).where(Conversation.product_id.in_(ids)))
            await session.execute(delete(Lead).where(Lead.product_id.in_(ids)))
            await session.execute(delete(Product).where(Product.slug.in_(SLUGS)))
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


@pytest.mark.asyncio
async def test_rename_when_only_obsolete_exists(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        product = Product(slug="life-herba", display_name="Life Herba", is_active=False)
        session.add(product)
        await session.flush()
        session.add(
            Segment(
                product_id=product.id,
                slug="healthcare-professionals",
                display_name="Healthcare Professionals",
                display_order=10,
            )
        )
        await session.commit()

    async with db() as session:
        changes = await reconcile_life_herba_segment_aliases(session)
        await session.commit()

    assert any("renamed to healthcare-practitioners" in c for c in changes)
    async with db() as session:
        slugs = (
            (
                await session.execute(
                    select(Segment.slug)
                    .join(Product)
                    .where(Product.slug == "life-herba")
                    .order_by(Segment.slug)
                )
            )
            .scalars()
            .all()
        )
        assert slugs == ["healthcare-practitioners"]


@pytest.mark.asyncio
async def test_remaps_references_then_deletes_obsolete_duplicate(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session:
        product = Product(slug="life-herba", display_name="Life Herba", is_active=False)
        session.add(product)
        await session.flush()
        obsolete = Segment(
            product_id=product.id,
            slug="wellness-centers",
            display_name="Wellness Centers",
            display_order=20,
        )
        canonical = Segment(
            product_id=product.id,
            slug="wellness-centres",
            display_name="Wellness Centres",
            display_order=20,
        )
        session.add_all([obsolete, canonical])
        await session.flush()
        session.add(
            AIConfiguration(
                product_id=product.id,
                segment_id=obsolete.id,
                system_prompt="Obsolete override.",
            )
        )
        lead = Lead(
            product_id=product.id,
            phone_e164="+919876543210",
            full_name="Centre Contact",
            status=LeadStatus.NEW,
        )
        session.add(lead)
        await session.flush()
        conversation = Conversation(
            product_id=product.id,
            segment_id=obsolete.id,
            lead_id=lead.id,
            status=ConversationStatus.ACTIVE,
        )
        session.add(conversation)
        await session.flush()
        obsolete_id = obsolete.id
        canonical_id = canonical.id
        conversation_id = conversation.id
        product_id = product.id
        await session.commit()
        assert conversation_id is not None

    async with db() as session:
        changes = await reconcile_life_herba_segment_aliases(session)
        await session.commit()

    assert any("remapped to wellness-centres" in c for c in changes)
    async with db() as session:
        assert await session.get(Segment, obsolete_id) is None
        remaining = (
            (await session.execute(select(Segment.slug).where(Segment.product_id == product_id)))
            .scalars()
            .all()
        )
        assert "wellness-centers" not in remaining
        assert "wellness-centres" in remaining
        reloaded = await session.get(Conversation, conversation_id)
        assert reloaded is not None
        assert reloaded.segment_id == canonical_id
        leftover = (
            (
                await session.execute(
                    select(AIConfiguration).where(
                        AIConfiguration.system_prompt == "Obsolete override."
                    )
                )
            )
            .scalars()
            .all()
        )
        assert leftover == []


@pytest.mark.asyncio
async def test_other_products_are_untouched(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        other = Product(slug="other-product", display_name="Other", is_active=False)
        session.add(other)
        await session.flush()
        session.add(
            Segment(
                product_id=other.id,
                slug="healthcare-professionals",
                display_name="Keep me",
                display_order=1,
            )
        )
        session.add(Product(slug="life-herba", display_name="Life Herba", is_active=False))
        await session.commit()

    async with db() as session:
        await reconcile_life_herba_segment_aliases(session)
        await session.commit()

    async with db() as session:
        other_slugs = (
            (
                await session.execute(
                    select(Segment.slug).join(Product).where(Product.slug == "other-product")
                )
            )
            .scalars()
            .all()
        )
        assert other_slugs == ["healthcare-professionals"]


@pytest.mark.asyncio
async def test_configure_products_collapses_obsolete_then_seeds_canonical(
    db: async_sessionmaker[AsyncSession], cli_env: None
) -> None:
    async with db() as session:
        product = Product(slug="life-herba", display_name="Life Herba", is_active=False)
        session.add(product)
        await session.flush()
        for obsolete in LIFE_HERBA_SEGMENT_RENAMES:
            session.add(
                Segment(
                    product_id=product.id,
                    slug=obsolete,
                    display_name=obsolete,
                    display_order=10,
                )
            )
        await session.commit()

    code = await configure_products(Settings(), SHIPPED, out=io.StringIO())
    assert code == EXIT_OK

    async with db() as session:
        slugs = (
            (
                await session.execute(
                    select(Segment.slug)
                    .join(Product)
                    .where(Product.slug == "life-herba")
                    .order_by(Segment.display_order, Segment.slug)
                )
            )
            .scalars()
            .all()
        )
        assert slugs == [
            "healthcare-practitioners",
            "wellness-centres",
            "pharmacies",
            "consumers",
        ]
        is_active = (
            await session.execute(select(Product.is_active).where(Product.slug == "life-herba"))
        ).scalar_one()
        assert is_active is False
