"""SustainX obsolete segment remapping (canonical launch slugs).

Clear renames collapse onto the five launch segments. Ambiguous experimental segments
with references are preserved and reported; unreferenced leftovers are removed.
Other products are untouched. SustainX stays inactive.
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
    SUSTAINX_SEGMENT_RENAMES,
    reconcile_sustainx_segment_aliases,
)
from app.core.config import Settings, reset_settings_cache
from app.domain.enums import ConversationStatus, LeadStatus
from app.persistence import database
from app.persistence.models import AIConfiguration, Conversation, Lead, Product, Segment
from tests.integration.helpers import get_test_database_url

pytestmark = pytest.mark.integration

SHIPPED = Path(__file__).resolve().parents[2] / "config" / "products.json"
SLUGS = ("sustainx", "life-herba", "other-product", "academicae")


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
        product = Product(slug="sustainx", display_name="SustainX", is_active=False)
        session.add(product)
        await session.flush()
        session.add(
            Segment(
                product_id=product.id,
                slug="government",
                display_name="Government",
                display_order=10,
            )
        )
        await session.commit()

    async with db() as session:
        changes = await reconcile_sustainx_segment_aliases(session)
        await session.commit()

    assert any("renamed to government-bodies" in c for c in changes)
    async with db() as session:
        slugs = (
            (
                await session.execute(
                    select(Segment.slug)
                    .join(Product)
                    .where(Product.slug == "sustainx")
                    .order_by(Segment.slug)
                )
            )
            .scalars()
            .all()
        )
        assert slugs == ["government-bodies"]


@pytest.mark.asyncio
async def test_remaps_references_then_deletes_obsolete_duplicate(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session:
        product = Product(slug="sustainx", display_name="SustainX", is_active=False)
        session.add(product)
        await session.flush()
        obsolete = Segment(
            product_id=product.id,
            slug="industry",
            display_name="Industry",
            display_order=20,
        )
        canonical = Segment(
            product_id=product.id,
            slug="industries",
            display_name="Industries",
            display_order=10,
        )
        session.add_all([obsolete, canonical])
        await session.flush()
        lead = Lead(
            product_id=product.id,
            phone_e164="+919876543210",
            full_name="Industry Contact",
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

    async with db() as session:
        changes = await reconcile_sustainx_segment_aliases(session)
        await session.commit()

    assert any("remapped to industries" in c for c in changes)
    async with db() as session:
        assert await session.get(Segment, obsolete_id) is None
        remaining = (
            (await session.execute(select(Segment.slug).where(Segment.product_id == product_id)))
            .scalars()
            .all()
        )
        assert "industry" not in remaining
        assert "industries" in remaining
        conversation = await session.get(Conversation, conversation_id)
        assert conversation is not None
        assert conversation.segment_id == canonical_id


@pytest.mark.asyncio
async def test_ambiguous_segment_with_references_is_preserved_and_reported(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session:
        product = Product(slug="sustainx", display_name="SustainX", is_active=False)
        session.add(product)
        await session.flush()
        ambiguous = Segment(
            product_id=product.id,
            slug="corporates",
            display_name="Corporates",
            display_order=99,
        )
        session.add(ambiguous)
        await session.flush()
        lead = Lead(
            product_id=product.id,
            phone_e164="+919876543211",
            full_name="Corp Contact",
            status=LeadStatus.NEW,
        )
        session.add(lead)
        await session.flush()
        session.add(
            Conversation(
                product_id=product.id,
                segment_id=ambiguous.id,
                lead_id=lead.id,
                status=ConversationStatus.ACTIVE,
            )
        )
        ambiguous_id = ambiguous.id
        await session.commit()

    async with db() as session:
        changes = await reconcile_sustainx_segment_aliases(session)
        await session.commit()

    assert any("ambiguous historical data" in c and "corporates" in c for c in changes)
    async with db() as session:
        assert await session.get(Segment, ambiguous_id) is not None


@pytest.mark.asyncio
async def test_unreferenced_non_canonical_segment_is_removed(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session:
        product = Product(slug="sustainx", display_name="SustainX", is_active=False)
        session.add(product)
        await session.flush()
        session.add(
            Segment(
                product_id=product.id,
                slug="msmes",
                display_name="MSMEs",
                display_order=99,
            )
        )
        await session.commit()

    async with db() as session:
        changes = await reconcile_sustainx_segment_aliases(session)
        await session.commit()

    assert any("msmes" in c and "removed" in c for c in changes)
    async with db() as session:
        slugs = (
            (
                await session.execute(
                    select(Segment.slug).join(Product).where(Product.slug == "sustainx")
                )
            )
            .scalars()
            .all()
        )
        assert "msmes" not in slugs


@pytest.mark.asyncio
async def test_other_products_are_untouched(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        other = Product(slug="other-product", display_name="Other", is_active=False)
        session.add(other)
        await session.flush()
        session.add(
            Segment(
                product_id=other.id,
                slug="industry",
                display_name="Keep me",
                display_order=1,
            )
        )
        session.add(Product(slug="sustainx", display_name="SustainX", is_active=False))
        await session.commit()

    async with db() as session:
        await reconcile_sustainx_segment_aliases(session)
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
        assert other_slugs == ["industry"]


@pytest.mark.asyncio
async def test_configure_products_collapses_obsolete_then_seeds_canonical(
    db: async_sessionmaker[AsyncSession], cli_env: None
) -> None:
    async with db() as session:
        product = Product(slug="sustainx", display_name="SustainX", is_active=False)
        session.add(product)
        await session.flush()
        for obsolete in SUSTAINX_SEGMENT_RENAMES:
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
                    .where(Product.slug == "sustainx")
                    .order_by(Segment.display_order, Segment.slug)
                )
            )
            .scalars()
            .all()
        )
        assert slugs == [
            "industries",
            "institutions",
            "government-bodies",
            "students",
            "professionals-executives",
        ]
        row = (
            await session.execute(select(Product).where(Product.slug == "sustainx"))
        ).scalar_one()
        assert row.is_active is False
        assert row.google_calendar_id is not None
        assert row.whatsapp_phone_number_id == "1355444384311332"
        assert row.whatsapp_business_account_id == "1107203798409996"
        ai = (
            await session.execute(
                select(AIConfiguration).where(
                    AIConfiguration.product_id == row.id,
                    AIConfiguration.segment_id.is_(None),
                )
            )
        ).scalar_one()
        assert ai.qualification_mode == "ai_judgement"
        assert ai.system_prompt
