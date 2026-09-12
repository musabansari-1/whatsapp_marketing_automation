"""Helpers for integration tests that drive the API through a sync TestClient.

`TestClient` is synchronous, so a test using it cannot itself be async. When such a
test needs to change database state directly, it needs a way to run a coroutine.

`run_db` does that on its own event loop with its own short-lived engine. Reusing the
session-scoped engine here would fail: asyncpg connections are bound to the loop that
created them.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable
from typing import TypeVar

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

T = TypeVar("T")

DEFAULT_TEST_DATABASE_URL = (
    "postgresql+asyncpg://postgres:postgres@localhost:5442/whatsapp_marketing_test"
)


def get_test_database_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)


def run_db(operation: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Run one database operation in its own loop, committing on success."""

    async def _run() -> T:
        engine = create_async_engine(get_test_database_url())
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            async with factory() as session:
                result = await operation(session)
                await session.commit()
                return result
        finally:
            await engine.dispose()

    return asyncio.run(_run())


#: The spreadsheet format a fixture product reads: the canonical names as headers, which is
#: what every upload test sends. Configuration on the product, as it is in production.
TEST_AUDIENCE_SCHEMA: list[dict[str, object]] = [
    {"source": "Name", "field": "name", "required": True},
    {"source": "Phone", "field": "phone", "required": True},
    {"source": "Email", "field": "email", "required": False},
    {"source": "Company", "field": "company", "required": False},
    {"source": "Designation", "field": "designation", "required": False},
    {"source": "City", "field": "city", "required": False},
]
TEST_SYSTEM_PROMPT = "You are the fixture product's assistant. Answer briefly and truthfully."
TEST_WHATSAPP_NUMBER = "fixture-phone-number-id"
TEST_WHATSAPP_ACCOUNT = "fixture-business-account"


async def configure_test_product(session: AsyncSession, product_id: uuid.UUID) -> None:
    """Give a fixture product what a configured product holds, wherever it holds nothing.

    Phase 27 removed the platform-wide fallbacks — the environment's audience schema,
    system prompt and WhatsApp number — so an unconfigured product now refuses uploads,
    replies and sends, exactly as it should. A test about uploading, replying or sending
    needs a product that is configured, the way a real one is.

    Fills only what is absent, so a test that configured something itself keeps it. A test
    about the *unconfigured* case builds its own product.
    """
    from sqlalchemy import select

    from app.persistence.models import AIConfiguration, Product

    product = await session.get(Product, product_id)
    assert product is not None
    if product.audience_column_schema is None:
        product.audience_column_schema = [dict(column) for column in TEST_AUDIENCE_SCHEMA]
    if product.whatsapp_business_account_id is None:
        product.whatsapp_business_account_id = TEST_WHATSAPP_ACCOUNT
    if product.whatsapp_phone_number_id is None:
        claimed = (
            await session.execute(
                select(Product.id).where(Product.whatsapp_phone_number_id == TEST_WHATSAPP_NUMBER)
            )
        ).first()
        if claimed is None:
            product.whatsapp_phone_number_id = TEST_WHATSAPP_NUMBER

    has_policy = (
        await session.execute(
            select(AIConfiguration.id).where(
                AIConfiguration.product_id == product_id, AIConfiguration.segment_id.is_(None)
            )
        )
    ).first()
    if has_policy is None:
        session.add(AIConfiguration(product_id=product_id, system_prompt=TEST_SYSTEM_PROMPT))
    await session.flush()


async def ensure_product_id(session: AsyncSession) -> uuid.UUID:
    """The product a test's rows belong to, creating one if the schema has none.

    Since Phase 19, `audiences.product_id` and `campaigns.product_id` are NOT NULL — a
    real database always has the product the migration seeds, but the suite builds its
    schema with `create_all`, so fixtures have to supply one.

    Reuses whatever product already exists rather than adding another, and never asserts
    which one it is: the slug and display name are configuration and a test that depended
    on them would make renaming the product a code change.

    The product is configured for tests (`configure_test_product`), because since Phase 27
    nothing outside a product's own row can stand in for its configuration.
    """
    from sqlalchemy import select

    from app.persistence.models import Product

    existing = (
        await session.execute(select(Product).order_by(Product.created_at).limit(1))
    ).scalar_one_or_none()
    if existing is not None:
        await configure_test_product(session, existing.id)
        return existing.id

    product = Product(slug=f"fixture-{uuid.uuid4().hex[:8]}", display_name="Fixture product")
    session.add(product)
    await session.flush()
    await configure_test_product(session, product.id)
    return product.id


def scoped_prefix(product_id: uuid.UUID | None = None) -> str:
    """The dashboard API prefix for a product: `/api/v1/p/{slug}` (Phase 26).

    Every product-scoped route lives under its product's slug. With no product given, this
    is the test's default product — the one `ensure_product_id` creates or reuses — which
    is what a single-product test means. The slug is looked up rather than assumed,
    because slugs are data.
    """
    from app.api.main import API_V1_PREFIX
    from app.persistence.models import Product

    async def slug(session: AsyncSession) -> str:
        chosen = product_id or await ensure_product_id(session)
        product = await session.get(Product, chosen)
        assert product is not None, f"no product {chosen}"
        return product.slug

    return f"{API_V1_PREFIX}/p/{run_db(slug)}"


async def ensure_receiving_number(session: AsyncSession, phone_number_id: str) -> uuid.UUID:
    """A product that owns `phone_number_id`, so inbound routing can resolve it.

    Since Phase 23 the receiving number is the *only* inbound routing key ([D40]), and an
    unrecognised one is refused rather than guessed — so a fixture that posts a webhook
    has to configure one or every message it sends is correctly discarded.

    Attaches the number to the test's existing product instead of adding a second, which
    also keeps it within `uq_products_active_whatsapp_number`.
    """
    from app.persistence.models import Product

    product_id = await ensure_product_id(session)
    product = await session.get(Product, product_id)
    assert product is not None
    product.whatsapp_phone_number_id = phone_number_id
    await session.flush()
    return product_id
