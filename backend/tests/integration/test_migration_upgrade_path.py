"""The upgrade path, exercised for real (Phase 18).

Every other integration test builds its schema with `Base.metadata.create_all`, which
means the migrations are never executed and their data effects — the Academicae rename,
the Phase 18 backfill — are never proved. `create_all` produces the *destination* schema
directly; it cannot tell you whether an existing database gets there.

This module does the other thing. It creates a scratch database, migrates it to the
revision before Phase 18, writes rows that look like a real single-product deployment,
then upgrades and checks what happened to them.

Kept in its own database because the shared test database is dropped and recreated from
models once per session, and running migrations into it would fight that.

Everything here goes through asyncpg, the only driver installed. Each helper builds and
disposes its own engine: asyncpg connections are bound to the loop that created them,
which is the same reason `run_db` in `helpers.py` works this way.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.helpers import get_test_database_url

pytestmark = pytest.mark.integration

# The revision Phase 18 upgrades from, and the one it introduces.
BEFORE_PHASE_18 = "d3f8b21c60ae"
PHASE_18 = "e5c74a1b8d09"

# The row Phase 17 seeds and Phase 18 renames. Fixed so the backfill is unambiguous.
ACADEMICAE_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")

SCRATCH_DB = "whatsapp_marketing_migration_test"

BACKFILLED_TABLES = (
    "audiences",
    "campaigns",
    "conversations",
    "knowledge_bases",
    "leads",
    "messages",
    "bookings",
    "ai_runs",
    "whatsapp_templates",
    "notifications",
)


# ---------------------------------------------------------------- plumbing


def _url(database: str) -> str:
    base = get_test_database_url()
    return base.rsplit("/", 1)[0] + f"/{database}"


def _exec(url: str, statements: Sequence[tuple[str, dict[str, Any]]], *, autocommit: bool) -> None:
    async def go() -> None:
        engine = create_async_engine(
            url, isolation_level="AUTOCOMMIT" if autocommit else "READ COMMITTED"
        )
        try:
            async with engine.begin() as connection:
                for statement, params in statements:
                    await connection.execute(text(statement), params)
        finally:
            await engine.dispose()

    asyncio.run(go())


def _rows(url: str, statement: str, params: dict[str, Any] | None = None) -> list[Any]:
    async def go() -> list[Any]:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(text(statement), params or {})
                return list(result.all())
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _scalar(url: str, statement: str, params: dict[str, Any] | None = None) -> Any:
    return _rows(url, statement, params)[0][0]


def _alembic_config(url: str) -> Any:
    from alembic.config import Config

    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _alembic(url: str, revision: str, *, direction: str) -> None:
    """Run Alembic against the scratch database.

    `env.py` reads its URL from settings, so the environment variable is what steers it.
    The settings cache is reset on the way in and out so the scratch URL cannot leak
    into anything else in the session.
    """
    from alembic import command
    from app.core.config import reset_settings_cache

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    reset_settings_cache()
    try:
        if direction == "up":
            command.upgrade(_alembic_config(url), revision)
        else:
            command.downgrade(_alembic_config(url), revision)
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        reset_settings_cache()


@pytest.fixture(scope="module")
def scratch_database() -> Iterator[str]:
    """A database of our own, migrated by Alembic rather than built from models."""
    admin = _url("postgres")
    try:
        _exec(
            admin,
            [
                (f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)', {}),
                (f'CREATE DATABASE "{SCRATCH_DB}"', {}),
            ],
            autocommit=True,
        )
    except Exception as exc:  # noqa: BLE001 - no database means skip, as elsewhere
        pytest.skip(f"cannot create the scratch database: {type(exc).__name__}: {exc}")

    url = _url(SCRATCH_DB)
    _exec(url, [("CREATE EXTENSION IF NOT EXISTS vector", {})], autocommit=True)

    yield url

    _exec(admin, [(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)', {})], autocommit=True)


def _seed_pre_phase_18(url: str) -> dict[str, uuid.UUID]:
    """Rows as a real single-product deployment would hold them.

    Raw SQL against the *old* schema on purpose: the ORM models describe the new one,
    and using them here would quietly test the destination rather than the journey.
    """
    ids = {name: uuid.uuid4() for name in ("audience", "lead", "template", "campaign", "kb")}

    _exec(
        url,
        [
            (
                "INSERT INTO audiences (id, name, status, total_rows, valid_rows,"
                " invalid_rows, duplicate_rows)"
                " VALUES (:id, 'Legacy audience', 'CONFIRMED', 1, 1, 0, 0)",
                {"id": ids["audience"]},
            ),
            (
                "INSERT INTO leads (id, phone_e164, full_name, status, qualification_data,"
                " opted_out, source_audience_id)"
                " VALUES (:id, '+919876500900', 'Legacy lead', 'NEW', '{}', false, :audience)",
                {"id": ids["lead"], "audience": ids["audience"]},
            ),
            (
                "INSERT INTO whatsapp_templates (id, name, language, provider_template_id,"
                " provider_status, category, variable_count)"
                " VALUES (:id, 'legacy_promo', 'en', 'tpl-legacy', 'APPROVED', 'MARKETING', 1)",
                {"id": ids["template"]},
            ),
            (
                "INSERT INTO campaigns (id, name, status, audience_id, template_id,"
                " total_recipients, sent_count, delivered_count, read_count, failed_count,"
                " skipped_count)"
                " VALUES (:id, 'Legacy campaign', 'READY', :audience, :template,"
                " 1, 0, 0, 0, 0, 0)",
                {"id": ids["campaign"], "audience": ids["audience"], "template": ids["template"]},
            ),
            (
                "INSERT INTO knowledge_bases (id, name, description)"
                " VALUES (:id, 'Legacy knowledge', 'Everything we knew')",
                {"id": ids["kb"]},
            ),
        ],
        autocommit=False,
    )
    return ids


# ---------------------------------------------------------------- tests


class TestUpgradePath:
    """A pre-Phase-18 database, migrated forward."""

    def test_the_migration_renames_backfills_and_preserves(self, scratch_database: str) -> None:
        # ---- arrive at the schema as it was before this phase ----------------
        _alembic(scratch_database, BEFORE_PHASE_18, direction="up")

        seeded = _rows(scratch_database, "SELECT id, slug, display_name FROM products")
        assert len(seeded) == 1
        assert seeded[0][0] == ACADEMICAE_ID
        assert seeded[0][1] == "product-1", "Phase 17 seeds the placeholder identity"

        ids = _seed_pre_phase_18(scratch_database)

        # ---- the migration under test ---------------------------------------
        _alembic(scratch_database, PHASE_18, direction="up")

        products = _rows(scratch_database, "SELECT id, slug, display_name FROM products")

        # Exactly one product, renamed in place. A second row would mean Academicae had
        # been created alongside the placeholder rather than from it.
        assert len(products) == 1
        assert products[0][0] == ACADEMICAE_ID, "the fixed UUID must not change"
        assert products[0][1] == "academicae"
        assert products[0][2] == "Academicae"

        # Every scoped table backfilled, with nothing left behind.
        for table in BACKFILLED_TABLES:
            orphaned = _scalar(
                scratch_database,
                f"SELECT count(*) FROM {table} WHERE product_id IS NULL",  # noqa: S608
            )
            assert orphaned == 0, f"{table} has rows with no product after the backfill"

        # The seeded rows are still there, still themselves, now scoped.
        phone, name, product_id = _rows(
            scratch_database,
            "SELECT phone_e164, full_name, product_id FROM leads WHERE id = :id",
            {"id": ids["lead"]},
        )[0]
        assert phone == "+919876500900"
        assert name == "Legacy lead"
        assert product_id == ACADEMICAE_ID

        c_name, c_status, c_product, c_segment = _rows(
            scratch_database,
            "SELECT name, status, product_id, segment_id FROM campaigns WHERE id = :id",
            {"id": ids["campaign"]},
        )[0]
        assert c_name == "Legacy campaign"
        assert c_status == "READY"
        assert c_product == ACADEMICAE_ID
        assert c_segment is None, "no segment may be invented during migration"

    def test_no_segment_is_invented_anywhere(self, scratch_database: str) -> None:
        """Existing rows carry no segment information, so the migration must not
        manufacture any — and must not create a placeholder segment to avoid NULLs."""
        assert _scalar(scratch_database, "SELECT count(*) FROM segments") == 0

        for table in ("audiences", "campaigns", "conversations", "knowledge_bases"):
            attributed = _scalar(
                scratch_database,
                f"SELECT count(*) FROM {table} WHERE segment_id IS NOT NULL",  # noqa: S608
            )
            assert attributed == 0, f"{table} gained a segment it was never given"

    def test_existing_uniqueness_is_not_weakened(self, scratch_database: str) -> None:
        """The refinement this phase was asked for.

        A product-scoped unique over a nullable `product_id` would let two NULL-product
        rows share a name, because SQL treats NULLs as distinct. Both global constraints
        therefore stay until the phases that make their writers product-aware — Phase 26
        for knowledge, Phase 23 for templates.
        """
        with pytest.raises(IntegrityError):
            _exec(
                scratch_database,
                [
                    (
                        "INSERT INTO knowledge_bases (name, description)"
                        " VALUES ('Legacy knowledge', 'a second base with the same name')",
                        {},
                    )
                ],
                autocommit=False,
            )

        with pytest.raises(IntegrityError):
            _exec(
                scratch_database,
                [
                    (
                        "INSERT INTO whatsapp_templates (name, language,"
                        " provider_template_id, provider_status, category, variable_count)"
                        " VALUES ('legacy_promo', 'en', 'tpl-other', 'APPROVED',"
                        " 'MARKETING', 1)",
                        {},
                    )
                ],
                autocommit=False,
            )

    def test_a_segment_cannot_cross_products(self, scratch_database: str) -> None:
        """§28.8 in the migrated schema, not only in the model metadata."""
        other_product = uuid.uuid4()
        foreign_segment = uuid.uuid4()

        _exec(
            scratch_database,
            [
                (
                    "INSERT INTO products (id, slug, display_name)"
                    " VALUES (:id, 'other', 'Other product')",
                    {"id": other_product},
                ),
                (
                    "INSERT INTO segments (id, product_id, slug, display_name)"
                    " VALUES (:id, :product, 'government', 'Government')",
                    {"id": foreign_segment, "product": other_product},
                ),
            ],
            autocommit=False,
        )

        with pytest.raises(IntegrityError):
            _exec(
                scratch_database,
                [
                    (
                        "INSERT INTO audiences (name, status, total_rows, valid_rows,"
                        " invalid_rows, duplicate_rows, product_id, segment_id)"
                        " VALUES ('Crossed', 'CONFIRMED', 0, 0, 0, 0, :product, :segment)",
                        {"product": ACADEMICAE_ID, "segment": foreign_segment},
                    )
                ],
                autocommit=False,
            )

        _exec(
            scratch_database,
            [
                ("DELETE FROM segments WHERE id = :id", {"id": foreign_segment}),
                ("DELETE FROM products WHERE id = :id", {"id": other_product}),
            ],
            autocommit=False,
        )

    def test_the_migration_reverses(self, scratch_database: str) -> None:
        """Down and up again, ending where it started — including the rename.

        A downgrade that left the product called Academicae while the scope columns were
        gone would be a half-reversed migration, which is worse than one that does not
        reverse at all.
        """
        _alembic(scratch_database, BEFORE_PHASE_18, direction="down")

        assert _scalar(scratch_database, "SELECT slug FROM products") == "product-1"
        assert (
            _scalar(
                scratch_database,
                "SELECT count(*) FROM information_schema.columns"
                " WHERE table_name = 'leads' AND column_name = 'product_id'",
            )
            == 0
        )

        _alembic(scratch_database, PHASE_18, direction="up")

        assert _scalar(scratch_database, "SELECT slug FROM products") == "academicae"
        assert _scalar(scratch_database, "SELECT count(*) FROM leads WHERE product_id IS NULL") == 0
