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
from datetime import UTC, datetime, timedelta
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
PHASE_19 = "f2a9c31e7b45"

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


class TestPhase19OneWayDoor:
    """Product-scoped lead identity, and the boundary where it stops reversing.

    [D41] is called a one-way door for a concrete reason, and this pins where the door
    actually closes rather than asserting it in prose.
    """

    def test_the_downgrade_works_while_one_product_holds_the_phone(
        self, scratch_database: str
    ) -> None:
        """The state this migration is applied in, so the reversal must work there."""
        _alembic(scratch_database, PHASE_19, direction="up")

        _exec(
            scratch_database,
            [
                (
                    "INSERT INTO leads (phone_e164, status, qualification_data, opted_out,"
                    " product_id) VALUES ('+919876540001', 'NEW', '{}', false, :product)",
                    {"product": ACADEMICAE_ID},
                )
            ],
            autocommit=False,
        )

        _alembic(scratch_database, PHASE_18, direction="down")

        # The global rule is back, and the row survived.
        assert (
            _scalar(
                scratch_database,
                "SELECT count(*) FROM leads WHERE phone_e164 = '+919876540001'",
            )
            == 1
        )

        _alembic(scratch_database, PHASE_19, direction="up")

    def test_the_downgrade_refuses_once_two_products_hold_one_phone(
        self, scratch_database: str
    ) -> None:
        """The door closing.

        Two products holding one number is exactly what [D41] exists to allow, and it is
        also what a global unique on the phone cannot represent. The downgrade must fail
        rather than decide which product's status, qualification and conversation history
        to discard — a choice nothing in the data can make.
        """
        second = uuid.uuid4()
        _exec(
            scratch_database,
            [
                (
                    "INSERT INTO products (id, slug, display_name)"
                    " VALUES (:id, 'second-product', 'Second product')",
                    {"id": second},
                ),
                (
                    "INSERT INTO leads (phone_e164, status, qualification_data, opted_out,"
                    " product_id) VALUES ('+919876540002', 'NEW', '{}', false, :product)",
                    {"product": ACADEMICAE_ID},
                ),
                (
                    "INSERT INTO leads (phone_e164, status, qualification_data, opted_out,"
                    " product_id) VALUES ('+919876540002', 'NEW', '{}', false, :product)",
                    {"product": second},
                ),
            ],
            autocommit=False,
        )

        # Both rows exist, which is the point of the phase.
        assert (
            _scalar(
                scratch_database,
                "SELECT count(*) FROM leads WHERE phone_e164 = '+919876540002'",
            )
            == 2
        )

        with pytest.raises(IntegrityError):
            _alembic(scratch_database, PHASE_18, direction="down")

        # Nothing was lost in the attempt.
        assert (
            _scalar(
                scratch_database,
                "SELECT count(*) FROM leads WHERE phone_e164 = '+919876540002'",
            )
            == 2
        )

        _exec(
            scratch_database,
            [
                ("DELETE FROM leads WHERE phone_e164 = '+919876540002'", {}),
                ("DELETE FROM products WHERE id = :id", {"id": second}),
            ],
            autocommit=False,
        )

    def test_the_transitional_lane_rejects_duplicates(self, scratch_database: str) -> None:
        """A lead with no product yet is still one lead per number.

        Without the partial index over `product_id IS NULL` this would be unconstrained,
        because SQL treats NULLs as distinct — the exact hole a plain
        UNIQUE (product_id, phone_e164) would have left open.
        """
        _exec(
            scratch_database,
            [
                (
                    "INSERT INTO leads (phone_e164, status, qualification_data, opted_out)"
                    " VALUES ('+919876540003', 'NEW', '{}', false)",
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
                        "INSERT INTO leads (phone_e164, status, qualification_data,"
                        " opted_out) VALUES ('+919876540003', 'NEW', '{}', false)",
                        {},
                    )
                ],
                autocommit=False,
            )


# ---------------------------------------------------------------- Phase 24

PHASE_23 = "a4e81b2f7c93"
TEMPLATE_OWNERSHIP = "b3c8d1e5f927"
PHASE_24 = "c9e4a7b2d1f6"

TUESDAY_10AM_IST = datetime(2026, 9, 8, 4, 30, tzinfo=UTC)
TUESDAY_11AM_IST = datetime(2026, 9, 8, 5, 30, tzinfo=UTC)
HALF_HOUR = timedelta(minutes=30)
OTHER_PRODUCT = uuid.UUID("00000000-0000-4000-8000-0000000000b2")

LEAD_SQL = (
    "INSERT INTO leads (id, phone_e164, status, qualification_data, opted_out, product_id)"
    " VALUES (:id, :phone, 'NEW', '{}', false, :product)"
)
BOOKING_SQL = (
    "INSERT INTO bookings (lead_id, conversation_id, calendar_event_id, start_time, end_time,"
    " timezone, status, product_id)"
    " VALUES (:lead, :conversation, :event, :start, :end, 'Asia/Kolkata', :status, :product)"
)


def _booking(
    lead: uuid.UUID,
    event: str,
    start: datetime,
    *,
    conversation: uuid.UUID | None = None,
    product: uuid.UUID | None = None,
    status: str = "CONFIRMED",
) -> tuple[str, dict[str, Any]]:
    return (
        BOOKING_SQL,
        {
            "lead": lead,
            "conversation": conversation,
            "event": event,
            "start": start,
            "end": start + HALF_HOUR,
            "status": status,
            "product": product,
        },
    )


class TestPhase24BookingOwnership:
    """Bookings gain a product derived from their own parents, and the confirmed-slot
    rule becomes per product. Continues from the state the classes above leave."""

    lead_with_conversation = uuid.UUID("00000000-0000-4000-8000-0000000024a1")
    lead_without_conversation = uuid.UUID("00000000-0000-4000-8000-0000000024a2")
    conversation = uuid.UUID("00000000-0000-4000-8000-0000000024c1")

    def test_unfiled_bookings_take_the_product_of_their_parents(
        self, scratch_database: str
    ) -> None:
        """Bookings written between Phase 18 and 24 carry no product. Their conversation
        says which, or their lead when the conversation has been deleted: the booking's
        own parents, not a guess."""
        # The transitional-lane test above leaves a lead with no product, which Phase 23
        # rightly refuses to migrate past. It is a test artifact, so it goes.
        _exec(
            scratch_database, [("DELETE FROM leads WHERE product_id IS NULL", {})], autocommit=False
        )
        _alembic(scratch_database, PHASE_23, direction="up")

        _exec(
            scratch_database,
            [
                (
                    LEAD_SQL,
                    {
                        "id": self.lead_with_conversation,
                        "phone": "+919876541001",
                        "product": ACADEMICAE_ID,
                    },
                ),
                (
                    LEAD_SQL,
                    {
                        "id": self.lead_without_conversation,
                        "phone": "+919876541002",
                        "product": ACADEMICAE_ID,
                    },
                ),
                (
                    "INSERT INTO conversations (id, lead_id, status, product_id)"
                    " VALUES (:id, :lead, 'ACTIVE', :product)",
                    {
                        "id": self.conversation,
                        "lead": self.lead_with_conversation,
                        "product": ACADEMICAE_ID,
                    },
                ),
                _booking(
                    self.lead_with_conversation,
                    "evt-p24-conversation",
                    TUESDAY_10AM_IST,
                    conversation=self.conversation,
                ),
                _booking(self.lead_without_conversation, "evt-p24-lead-only", TUESDAY_11AM_IST),
            ],
            autocommit=False,
        )

        _alembic(scratch_database, PHASE_24, direction="up")

        assert _rows(
            scratch_database,
            "SELECT calendar_event_id, product_id FROM bookings"
            " WHERE calendar_event_id LIKE 'evt-p24-%' ORDER BY calendar_event_id",
        ) == [("evt-p24-conversation", ACADEMICAE_ID), ("evt-p24-lead-only", ACADEMICAE_ID)]
        assert (
            _scalar(
                scratch_database,
                "SELECT is_nullable FROM information_schema.columns"
                " WHERE table_name = 'bookings' AND column_name = 'product_id'",
            )
            == "NO"
        )

    def test_every_product_may_confirm_the_same_hour(self, scratch_database: str) -> None:
        other_lead = uuid.uuid4()
        _exec(
            scratch_database,
            [
                (
                    "INSERT INTO products (id, slug, display_name, google_calendar_id)"
                    " VALUES (:id, 'phase24-other', 'Other', 'other@calendar.example')",
                    {"id": OTHER_PRODUCT},
                ),
                (LEAD_SQL, {"id": other_lead, "phone": "+919876541001", "product": OTHER_PRODUCT}),
                # The hour Academicae already confirmed, on another calendar.
                _booking(other_lead, "evt-p24-other", TUESDAY_10AM_IST, product=OTHER_PRODUCT),
            ],
            autocommit=False,
        )

        # Within one product the hour is still taken.
        with pytest.raises(IntegrityError):
            _exec(
                scratch_database,
                [
                    _booking(
                        self.lead_without_conversation,
                        "evt-p24-double",
                        TUESDAY_10AM_IST,
                        product=ACADEMICAE_ID,
                    )
                ],
                autocommit=False,
            )

    def test_the_downgrade_refuses_while_two_products_hold_one_hour(
        self, scratch_database: str
    ) -> None:
        """The global rule cannot represent what this phase allows. The downgrade fails,
        and no booking is cancelled or deleted to make room."""
        with pytest.raises(IntegrityError):
            _alembic(scratch_database, TEMPLATE_OWNERSHIP, direction="down")

        assert (
            _scalar(
                scratch_database,
                "SELECT count(*) FROM bookings WHERE calendar_event_id LIKE 'evt-p24-%'",
            )
            == 3
        )

        _exec(
            scratch_database,
            [
                ("DELETE FROM bookings WHERE calendar_event_id = 'evt-p24-other'", {}),
                ("DELETE FROM leads WHERE product_id = :id", {"id": OTHER_PRODUCT}),
            ],
            autocommit=False,
        )

        # With one product per hour again, it reverses, and comes back.
        _alembic(scratch_database, TEMPLATE_OWNERSHIP, direction="down")
        assert (
            _scalar(
                scratch_database,
                "SELECT is_nullable FROM information_schema.columns"
                " WHERE table_name = 'bookings' AND column_name = 'product_id'",
            )
            == "YES"
        )
        _alembic(scratch_database, PHASE_24, direction="up")

    def test_disagreeing_parents_stop_the_upgrade(self, scratch_database: str) -> None:
        """A booking filed under one product while its conversation belongs to another.
        Nothing in the data says which is right, so the migration names it and stops."""
        _alembic(scratch_database, TEMPLATE_OWNERSHIP, direction="down")

        _exec(
            scratch_database,
            [
                _booking(
                    self.lead_with_conversation,
                    "evt-p24-wrong",
                    TUESDAY_11AM_IST,
                    conversation=self.conversation,
                    product=OTHER_PRODUCT,
                    status="PENDING",
                )
            ],
            autocommit=False,
        )

        with pytest.raises(RuntimeError, match="different product from its conversation"):
            _alembic(scratch_database, PHASE_24, direction="up")

        # Nothing was rewritten in the attempt.
        assert (
            _scalar(
                scratch_database,
                "SELECT product_id FROM bookings WHERE calendar_event_id = 'evt-p24-wrong'",
            )
            == OTHER_PRODUCT
        )

        _exec(
            scratch_database,
            [("DELETE FROM bookings WHERE calendar_event_id = 'evt-p24-wrong'", {})],
            autocommit=False,
        )
        _alembic(scratch_database, PHASE_24, direction="up")


# ---------------------------------------------------------------- Phase 26

PHASE_26 = "e8a3c5d2f1b7"

KNOWLEDGE_NULLABLE_SQL = (
    "SELECT is_nullable FROM information_schema.columns"
    " WHERE table_name = 'knowledge_bases' AND column_name = 'product_id'"
)


class TestPhase26KnowledgeOwnership:
    """Knowledge bases gain a mandatory owner, and names become unique per product.
    Continues from the state the Phase 24 class leaves: at Phase 24, with a second product."""

    def test_an_unowned_knowledge_base_stops_the_upgrade(self, scratch_database: str) -> None:
        """A knowledge base has no parent to take a product from, so an unowned one is
        named and refused — which product's assistant answers from it is not something
        the data can say."""
        _exec(
            scratch_database,
            [
                (
                    "INSERT INTO knowledge_bases (name, description)"
                    " VALUES ('Unowned notes', 'created before Phase 26')",
                    {},
                )
            ],
            autocommit=False,
        )

        with pytest.raises(RuntimeError, match="have no product"):
            _alembic(scratch_database, PHASE_26, direction="up")

        # Nothing was attributed in the attempt.
        assert (
            _scalar(
                scratch_database, "SELECT count(*) FROM knowledge_bases WHERE product_id IS NULL"
            )
            == 1
        )

        _exec(
            scratch_database,
            [("DELETE FROM knowledge_bases WHERE name = 'Unowned notes'", {})],
            autocommit=False,
        )
        _alembic(scratch_database, PHASE_26, direction="up")
        assert _scalar(scratch_database, KNOWLEDGE_NULLABLE_SQL) == "NO"

    def test_two_products_may_share_a_knowledge_base_name(self, scratch_database: str) -> None:
        """'Legacy knowledge' is Academicae's from the Phase 18 backfill. Another product
        may keep one of the same name; the same product may not keep two."""
        _exec(
            scratch_database,
            [
                (
                    "INSERT INTO knowledge_bases (name, description, product_id)"
                    " VALUES ('Legacy knowledge', 'the other product''s', :product)",
                    {"product": OTHER_PRODUCT},
                )
            ],
            autocommit=False,
        )

        with pytest.raises(IntegrityError):
            _exec(
                scratch_database,
                [
                    (
                        "INSERT INTO knowledge_bases (name, description, product_id)"
                        " VALUES ('Legacy knowledge', 'a second one', :product)",
                        {"product": ACADEMICAE_ID},
                    )
                ],
                autocommit=False,
            )

    def test_the_downgrade_refuses_while_two_products_share_a_name(
        self, scratch_database: str
    ) -> None:
        """The global name rule cannot represent what this phase allows. Nothing is renamed
        or deleted to make room, because either would change what an assistant knows."""
        with pytest.raises(IntegrityError):
            _alembic(scratch_database, PHASE_24, direction="down")

        assert (
            _scalar(
                scratch_database,
                "SELECT count(*) FROM knowledge_bases WHERE name = 'Legacy knowledge'",
            )
            == 2
        )

        _exec(
            scratch_database,
            [
                (
                    "DELETE FROM knowledge_bases WHERE name = 'Legacy knowledge' AND product_id = :p",
                    {"p": OTHER_PRODUCT},
                )
            ],
            autocommit=False,
        )
        _alembic(scratch_database, PHASE_24, direction="down")
        assert _scalar(scratch_database, KNOWLEDGE_NULLABLE_SQL) == "YES"
        _alembic(scratch_database, PHASE_26, direction="up")


# ---------------------------------------------------------------- Phase 27

PHASE_27 = "f4a1c7e9b2d5"

AUDIENCE_SCHEMA_COLUMN_SQL = (
    "SELECT is_nullable FROM information_schema.columns"
    " WHERE table_name = 'products' AND column_name = 'audience_column_schema'"
)
CONFIGURED_SCHEMAS_SQL = "SELECT count(*) FROM products WHERE audience_column_schema IS NOT NULL"


class TestPhase27AudienceSchemas:
    """Each product gains its own audience column schema. Schema only: the migration copies
    nothing from any environment and gives no product a mapping. Continues from the state
    the Phase 26 class leaves: at Phase 26."""

    def test_the_upgrade_adds_an_empty_nullable_column(self, scratch_database: str) -> None:
        _alembic(scratch_database, PHASE_27, direction="up")

        assert _scalar(scratch_database, AUDIENCE_SCHEMA_COLUMN_SQL) == "YES"
        assert _scalar(scratch_database, CONFIGURED_SCHEMAS_SQL) == 0

    def test_only_a_list_of_mappings_fits_the_column(self, scratch_database: str) -> None:
        with pytest.raises(IntegrityError):
            _exec(
                scratch_database,
                [
                    (
                        "UPDATE products SET audience_column_schema ="
                        " CAST(:schema AS jsonb) WHERE id = :id",
                        {"schema": '{"columns": []}', "id": ACADEMICAE_ID},
                    )
                ],
                autocommit=False,
            )

    def test_the_downgrade_refuses_to_discard_a_configured_schema(
        self, scratch_database: str
    ) -> None:
        _exec(
            scratch_database,
            [
                (
                    "UPDATE products SET audience_column_schema = CAST(:schema AS jsonb)"
                    " WHERE id = :id",
                    {"schema": "[]", "id": ACADEMICAE_ID},
                )
            ],
            autocommit=False,
        )

        with pytest.raises(RuntimeError, match="audience column schema configured"):
            _alembic(scratch_database, PHASE_26, direction="down")
        assert _scalar(scratch_database, CONFIGURED_SCHEMAS_SQL) == 1

        _exec(
            scratch_database,
            [("UPDATE products SET audience_column_schema = NULL", {})],
            autocommit=False,
        )
        _alembic(scratch_database, PHASE_26, direction="down")
        assert _rows(scratch_database, AUDIENCE_SCHEMA_COLUMN_SQL) == []
        _alembic(scratch_database, PHASE_27, direction="up")


# ---------------------------------------------------------------- Academicae launch safety

ACADEMICAE_LAUNCH = "a1b2c3d4e5f6"

ACADEMICAE_ACTIVE_SQL = "SELECT is_active FROM products WHERE id = CAST(:id AS uuid)"
MIN_LEAD_COLUMN_SQL = (
    "SELECT is_nullable FROM information_schema.columns"
    " WHERE table_name = 'products' AND column_name = 'demo_min_lead_days'"
)
QUALIFICATION_MODE_COLUMN_SQL = (
    "SELECT is_nullable FROM information_schema.columns"
    " WHERE table_name = 'ai_configurations' AND column_name = 'qualification_mode'"
)


def _current_revision(url: str) -> str | None:
    try:
        rows = _rows(url, "SELECT version_num FROM alembic_version")
    except Exception:  # noqa: BLE001 - empty scratch DB has no alembic_version yet
        return None
    return rows[0][0] if rows else None


def _ensure_revision(url: str, revision: str) -> None:
    """Move the scratch DB to `revision`, whether we are behind or ahead of it."""
    current = _current_revision(url)
    if current == revision:
        return
    if current is None:
        _alembic(url, revision, direction="up")
        return
    try:
        _alembic(url, revision, direction="up")
    except Exception:  # noqa: BLE001 - destination is behind the current head
        _alembic(url, revision, direction="down")


class TestAcademicaeLaunchLeavesProductInactive:
    """Migration `a1b2c3d4e5f6` turns off the Phase-17 active seed for Academicae.

    Self-contained against the module scratch DB: brings the schema to Phase 27 (where
    Academicae is still the Phase-17 active seed), then applies the launch revision.
    After that upgrade the product must be inactive so migrate → configure-products
    cannot go live without `activate-product`.
    """

    def test_upgrade_deactivates_academicae_by_fixed_id(self, scratch_database: str) -> None:
        _ensure_revision(scratch_database, PHASE_27)
        _exec(
            scratch_database,
            [
                (
                    "UPDATE products SET is_active = true WHERE id = CAST(:id AS uuid)",
                    {"id": ACADEMICAE_ID},
                )
            ],
            autocommit=False,
        )
        assert _scalar(scratch_database, ACADEMICAE_ACTIVE_SQL, {"id": ACADEMICAE_ID}) is True

        _alembic(scratch_database, ACADEMICAE_LAUNCH, direction="up")

        assert _scalar(scratch_database, MIN_LEAD_COLUMN_SQL) == "YES"
        assert _scalar(scratch_database, QUALIFICATION_MODE_COLUMN_SQL) == "YES"
        assert _scalar(scratch_database, ACADEMICAE_ACTIVE_SQL, {"id": ACADEMICAE_ID}) is False

    def test_downgrade_does_not_reactivate_academicae(self, scratch_database: str) -> None:
        _ensure_revision(scratch_database, ACADEMICAE_LAUNCH)
        assert _scalar(scratch_database, ACADEMICAE_ACTIVE_SQL, {"id": ACADEMICAE_ID}) is False

        _alembic(scratch_database, PHASE_27, direction="down")

        assert _scalar(scratch_database, ACADEMICAE_ACTIVE_SQL, {"id": ACADEMICAE_ID}) is False
        assert _rows(scratch_database, MIN_LEAD_COLUMN_SQL) == []

        _alembic(scratch_database, ACADEMICAE_LAUNCH, direction="up")
