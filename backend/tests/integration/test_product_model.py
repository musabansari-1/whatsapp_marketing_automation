"""Product, segment and AI configuration constraints (Phase 17, §28).

These are database-level tests on purpose. The invariants here cannot be enforced by
application validation alone — two workers, or one careless script, would defeat it —
and the most important of them is one a naive schema would miss entirely.

Nothing in the application reads these tables yet. Phase 18 scopes the existing data to
a product and Phase 22 implements AI configuration resolution; this phase proves the
shape those phases will depend on.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models import AIConfiguration, Product, Segment
from tests.integration.helpers import run_db

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    """Remove test products, leaving the seeded one alone.

    The seeded product is real data every later phase depends on, so these tests add
    their own and clear only those.
    """

    async def clear(session: AsyncSession) -> None:
        await session.execute(delete(Product).where(Product.slug.like("test-%")))

    run_db(clear)
    yield
    run_db(clear)


def make_product(slug: str = "test-alpha") -> uuid.UUID:
    async def create(session: AsyncSession) -> uuid.UUID:
        product = Product(slug=slug, display_name=f"Test {slug}")
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


class TestProducts:
    def test_the_fixture_provides_a_product(self, seeded_product: uuid.UUID) -> None:
        """Phase 18 backfills every existing row to a product, so one must exist.

        In a real database the migration seeds it. The suite builds its schema with
        `create_all`, so the `seeded_product` fixture stands in — and this asserts the
        fixture works, since every scoped test from Phase 18 will depend on it.

        Asserted by identity rather than by slug: no test may depend on what the product
        is called, because the slug is temporary configuration and the display name may
        change freely.
        """

        async def read(session: AsyncSession) -> Product | None:
            return await session.get(Product, seeded_product)

        product = run_db(read)
        assert product is not None
        assert product.is_active is True

    def test_slugs_are_unique(self) -> None:
        make_product("test-alpha")
        with pytest.raises(IntegrityError):
            make_product("test-alpha")

    def test_several_products_coexist(self) -> None:
        """The point of the whole architecture: a fourth product is an insert."""
        first = make_product("test-alpha")
        second = make_product("test-beta")
        third = make_product("test-gamma")
        assert len({first, second, third}) == 3


class TestSegments:
    def test_a_product_may_have_no_segments(self) -> None:
        """§28.2. Product 1 has none, and no placeholder is invented to satisfy the
        schema."""
        product_id = make_product()

        async def read(session: AsyncSession) -> int:
            result = await session.execute(select(Segment).where(Segment.product_id == product_id))
            return len(list(result.scalars().all()))

        assert run_db(read) == 0

    def test_a_slug_is_unique_within_a_product(self) -> None:
        product_id = make_product()
        make_segment(product_id, "government")
        with pytest.raises(IntegrityError):
            make_segment(product_id, "government")

    def test_two_products_may_share_a_slug(self) -> None:
        """Uniqueness is per product, not global: two products may each sell to
        government, and those are different segments."""
        first = make_product("test-alpha")
        second = make_product("test-beta")

        a = make_segment(first, "government")
        b = make_segment(second, "government")

        assert a != b

    def test_deleting_a_product_removes_its_segments(self) -> None:
        product_id = make_product()
        make_segment(product_id, "government")

        async def delete_product(session: AsyncSession) -> None:
            await session.execute(delete(Product).where(Product.id == product_id))

        run_db(delete_product)

        async def count(session: AsyncSession) -> int:
            result = await session.execute(select(Segment).where(Segment.product_id == product_id))
            return len(list(result.scalars().all()))

        assert run_db(count) == 0


class TestAIConfigurationUniqueness:
    """The invariant a naive schema would miss.

    `UNIQUE (product_id, segment_id)` looks sufficient and is not: SQL treats NULLs as
    distinct from one another, so a product could accumulate any number of rows claiming
    to be its default and resolution would silently pick one.
    """

    def test_only_one_product_default_is_possible(self) -> None:
        product_id = make_product()

        async def add_default(session: AsyncSession) -> None:
            session.add(AIConfiguration(product_id=product_id, segment_id=None))
            await session.flush()

        run_db(add_default)

        with pytest.raises(IntegrityError):
            run_db(add_default)

    def test_only_one_override_per_segment_is_possible(self) -> None:
        product_id = make_product()
        segment_id = make_segment(product_id, "government")

        async def add_override(session: AsyncSession) -> None:
            session.add(AIConfiguration(product_id=product_id, segment_id=segment_id))
            await session.flush()

        run_db(add_override)

        with pytest.raises(IntegrityError):
            run_db(add_override)

    def test_a_default_and_an_override_coexist(self) -> None:
        """The Life Herba shape: one product default plus sparse segment overrides."""
        product_id = make_product()
        pharmacies = make_segment(product_id, "pharmacies")
        consumers = make_segment(product_id, "consumers")

        async def add_all(session: AsyncSession) -> int:
            session.add(
                AIConfiguration(
                    product_id=product_id,
                    segment_id=None,
                    system_prompt="Ecosystem-wide instructions.",
                )
            )
            session.add(
                AIConfiguration(
                    product_id=product_id,
                    segment_id=pharmacies,
                    system_prompt="Pharmacy onboarding focus.",
                )
            )
            session.add(
                AIConfiguration(
                    product_id=product_id,
                    segment_id=consumers,
                    system_prompt="Consumer services focus.",
                )
            )
            await session.flush()
            result = await session.execute(
                select(AIConfiguration).where(AIConfiguration.product_id == product_id)
            )
            return len(list(result.scalars().all()))

        assert run_db(add_all) == 3

    def test_a_product_with_no_segment_overrides_is_valid(self) -> None:
        """The SustainX shape: one product default, segments that exist for targeting
        only, and no AI configuration rows for them at all."""
        product_id = make_product()
        make_segment(product_id, "government")
        make_segment(product_id, "industry")

        async def add_default_only(session: AsyncSession) -> int:
            session.add(
                AIConfiguration(product_id=product_id, system_prompt="One behaviour for all.")
            )
            await session.flush()
            result = await session.execute(
                select(AIConfiguration).where(AIConfiguration.product_id == product_id)
            )
            return len(list(result.scalars().all()))

        assert run_db(add_default_only) == 1


class TestAIConfigurationIntegrity:
    def test_a_segment_override_cannot_cross_products(self) -> None:
        """§28.8 enforced as referential integrity, not application validation.

        A configuration pairing one product with another product's segment is not
        merely rejected by a service-layer check — it cannot be written.
        """
        first = make_product("test-alpha")
        second = make_product("test-beta")
        foreign_segment = make_segment(second, "government")

        async def add_crossed(session: AsyncSession) -> None:
            session.add(AIConfiguration(product_id=first, segment_id=foreign_segment))
            await session.flush()

        with pytest.raises(IntegrityError):
            run_db(add_crossed)

    def test_every_value_column_may_be_null(self) -> None:
        """NULL means inherit, which is what makes a segment override sparse."""
        product_id = make_product()
        segment_id = make_segment(product_id, "government")

        async def add_empty(session: AsyncSession) -> AIConfiguration:
            config = AIConfiguration(product_id=product_id, segment_id=segment_id)
            session.add(config)
            await session.flush()
            return config

        config = run_db(add_empty)
        assert config.system_prompt is None
        assert config.qualification_required_fields is None
        assert config.model is None
        assert config.rag_top_k is None
        assert config.fallback_message is None

    def test_empty_and_absent_qualification_fields_are_distinguishable(self) -> None:
        """The reason that column is JSONB rather than a delimited string.

        NULL inherits. An empty list is a deliberate statement that no field is
        required — under which, per [D37], no lead can qualify. A text column could not
        express both.
        """
        product_id = make_product()
        segment_id = make_segment(product_id, "government")

        async def add_both(session: AsyncSession) -> tuple[object, object]:
            default = AIConfiguration(product_id=product_id, qualification_required_fields=None)
            override = AIConfiguration(
                product_id=product_id,
                segment_id=segment_id,
                qualification_required_fields=[],
            )
            session.add_all([default, override])
            await session.flush()
            return default.qualification_required_fields, override.qualification_required_fields

        inherited, explicit = run_db(add_both)
        assert inherited is None
        assert explicit == []

    def test_deleting_a_product_removes_its_configuration(self) -> None:
        product_id = make_product()

        async def add_default(session: AsyncSession) -> None:
            session.add(AIConfiguration(product_id=product_id))
            await session.flush()

        run_db(add_default)

        async def delete_product(session: AsyncSession) -> None:
            await session.execute(delete(Product).where(Product.id == product_id))

        run_db(delete_product)

        async def count(session: AsyncSession) -> int:
            result = await session.execute(
                select(AIConfiguration).where(AIConfiguration.product_id == product_id)
            )
            return len(list(result.scalars().all()))

        assert run_db(count) == 0
