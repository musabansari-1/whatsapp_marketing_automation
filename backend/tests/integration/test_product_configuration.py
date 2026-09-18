"""Product configuration, legacy import, readiness and activation (Phase 27).

Against real PostgreSQL, through the command functions an operator runs. The guarantees:

    configuration is idempotent, preserves IDs and never touches runtime data
    configuration never changes whether a product is active
    the legacy import copies what the platform ran with, into the product it is told
    readiness names what is missing; activation refuses until nothing blocks
    no product's AI policy reaches another, and none comes from the environment

Products here use their own slugs and are removed afterwards, including the three real ones
the shipped catalog names.
"""

from __future__ import annotations

import io
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.cli import (
    EXIT_FAILED,
    EXIT_OK,
    activate_product,
    configure_products,
    deactivate_product,
    import_legacy_config,
    product_readiness,
)
from app.configuration.legacy import FORMER_DEFAULT_SYSTEM_PROMPT, LegacyEnvironment
from app.core.config import Settings, reset_settings_cache
from app.domain.ai_config import AIConfigurationError
from app.domain.audience import DEVELOPMENT_PLACEHOLDER_COLUMNS
from app.domain.enums import AudienceStatus, DocumentStatus, TemplateCategory
from app.persistence import database
from app.persistence.models import (
    AIConfiguration,
    Audience,
    KnowledgeBase,
    KnowledgeDocument,
    Lead,
    Product,
    Segment,
    WhatsAppTemplate,
)
from app.persistence.repositories import ai_configurations as ai_config_repo
from tests.integration.helpers import get_test_database_url

pytestmark = pytest.mark.integration

SHIPPED_CATALOG = Path(__file__).resolve().parents[2] / "config" / "products.json"
ACADEMICAE_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
SLUGS = ("academicae", "sustainx", "life-herba", "cfg-ready", "cfg-other", "product-1")

CONFIRMED_SCHEMA = [
    {"source": "Full Name", "field": "name", "required": True},
    {"source": "Mobile Number", "field": "phone", "required": True},
]


# ---------------------------------------------------------------- fixtures


@pytest_asyncio.fixture
async def db(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def clear() -> None:
        async with factory() as session:
            ids = select(Product.id).where(Product.slug.in_(SLUGS))
            bases = select(KnowledgeBase.id).where(KnowledgeBase.product_id.in_(ids))
            await session.execute(
                delete(KnowledgeDocument).where(KnowledgeDocument.knowledge_base_id.in_(bases))
            )
            await session.execute(delete(KnowledgeBase).where(KnowledgeBase.product_id.in_(ids)))
            await session.execute(
                delete(WhatsAppTemplate).where(WhatsAppTemplate.product_id.in_(ids))
            )
            await session.execute(delete(Audience).where(Audience.product_id.in_(ids)))
            await session.execute(delete(Lead).where(Lead.product_id.in_(ids)))
            # Segments and AI configuration go with their product (ON DELETE CASCADE).
            await session.execute(delete(Product).where(Product.slug.in_(SLUGS)))
            await session.commit()

    await clear()
    yield factory
    await clear()


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the commands at the test database."""
    monkeypatch.setenv("DATABASE_URL", get_test_database_url())
    reset_settings_cache()
    database._engine = None  # noqa: SLF001
    database._session_factory = None  # noqa: SLF001
    yield
    database._engine = None  # noqa: SLF001
    database._session_factory = None  # noqa: SLF001
    reset_settings_cache()


def write_catalog(directory: Path, *products: dict[str, Any]) -> Path:
    path = directory / f"catalog-{uuid.uuid4().hex[:6]}.json"
    path.write_text(json.dumps({"products": list(products)}), encoding="utf-8")
    return path


def ready_entry(slug: str = "cfg-ready", **overrides: Any) -> dict[str, Any]:
    """A catalog entry holding every blocker-free value, all synthetic."""
    entry: dict[str, Any] = {
        "slug": slug,
        "display_name": slug.title(),
        "audience_column_schema": CONFIRMED_SCHEMA,
        "whatsapp_phone_number_id": f"{slug}-number",
        "whatsapp_business_account_id": "cfg-shared-account",
        "google_calendar_id": f"{slug}@calendar.example",
        "ai_configuration": {
            "system_prompt": f"The {slug} prompt.",
            "qualification_required_fields": ["interested"],
        },
    }
    entry.update(overrides)
    return entry


async def configure(tmp_path: Path, *products: dict[str, Any], **options: Any) -> tuple[int, str]:
    out = io.StringIO()
    code = await configure_products(
        Settings(), write_catalog(tmp_path, *products), out=out, **options
    )
    return code, out.getvalue()


async def product(factory: async_sessionmaker[AsyncSession], slug: str) -> Product | None:
    async with factory() as session:
        return (
            await session.execute(select(Product).where(Product.slug == slug))
        ).scalar_one_or_none()


async def give_campaign_template_and_knowledge(
    factory: async_sessionmaker[AsyncSession], slug: str
) -> None:
    """What turns READY WITH GAPS into READY: an approved marketing template it owns and a
    ready document."""
    async with factory() as session:
        owner = (await session.execute(select(Product).where(Product.slug == slug))).scalar_one()
        session.add(
            WhatsAppTemplate(
                name=f"{slug}_welcome",
                language="en",
                product_id=owner.id,
                provider_template_id=f"tpl-{uuid.uuid4()}",
                provider_status="APPROVED",
                category=TemplateCategory.MARKETING,
                variable_count=0,
            )
        )
        base = KnowledgeBase(name="Product knowledge", product_id=owner.id)
        session.add(base)
        await session.flush()
        session.add(
            KnowledgeDocument(
                knowledge_base_id=base.id,
                title="facts.md",
                source_filename="facts.md",
                content_type="text/markdown",
                byte_size=10,
                content_hash=uuid.uuid4().hex,
                status=DocumentStatus.READY,
            )
        )
        await session.commit()


# ---------------------------------------------------------------- configuration


class TestTheShippedCatalog:
    async def test_creates_the_confirmed_products_inactive_with_their_segments(
        self, cli_env: None, db: async_sessionmaker[AsyncSession]
    ) -> None:
        code = await configure_products(Settings(), SHIPPED_CATALOG, out=io.StringIO())
        assert code == EXIT_OK

        async with db() as session:
            rows = {
                p.slug: p
                for p in (
                    await session.execute(
                        select(Product).where(Product.slug.in_(["sustainx", "life-herba"]))
                    )
                ).scalars()
            }
            segments = {
                slug: [
                    s.slug
                    for s in (
                        await session.execute(
                            select(Segment)
                            .where(Segment.product_id == rows[slug].id)
                            .order_by(Segment.display_order)
                        )
                    ).scalars()
                ]
                for slug in rows
            }
            policies = await session.scalar(
                select(func.count())
                .select_from(AIConfiguration)
                .where(AIConfiguration.product_id.in_([p.id for p in rows.values()]))
            )

        assert {slug: row.is_active for slug, row in rows.items()} == {
            "sustainx": False,
            "life-herba": False,
        }
        assert segments == {
            "sustainx": ["government", "industry"],
            "life-herba": [
                "healthcare-practitioners",
                "wellness-centres",
                "pharmacies",
                "consumers",
            ],
        }
        # SustainX stays identity-only. Life Herba may carry confirmed AI / conversion
        # config, but still invents no WhatsApp number, calendar, or audience schema.
        assert policies == 5  # Life Herba: 1 product default + 4 segment overrides
        sustainx = rows["sustainx"]
        life_herba = rows["life-herba"]
        assert sustainx.whatsapp_phone_number_id is None
        assert sustainx.google_calendar_id is None
        assert sustainx.audience_column_schema is None
        assert life_herba.whatsapp_phone_number_id is None
        assert life_herba.google_calendar_id is None
        assert life_herba.audience_column_schema is None
        assert life_herba.conversion_mode == "external_registration"
        assert life_herba.registration_url == "https://lifeherbagroup.com/"

    async def test_keeps_the_existing_academicae_row_its_id_and_does_not_flip_activation(
        self, cli_env: None, db: async_sessionmaker[AsyncSession]
    ) -> None:
        """The catalog updates Academicae in place and never creates a second row.

        Activation is not configuration: an already-active row stays active, and the
        catalog has no `is_active` key that could flip it. (Fresh installs are inactive
        after migration `a1b2c3d4e5f6`; this case only proves the catalog itself.)
        """
        async with db() as session:
            session.add(
                Product(
                    id=ACADEMICAE_ID, slug="academicae", display_name="Academicae", is_active=True
                )
            )
            await session.commit()

        assert await configure_products(Settings(), SHIPPED_CATALOG, out=io.StringIO()) == EXIT_OK

        async with db() as session:
            rows = list(
                (
                    await session.execute(select(Product).where(Product.slug == "academicae"))
                ).scalars()
            )
        assert [(row.id, row.is_active) for row in rows] == [(ACADEMICAE_ID, True)]

    async def test_shipped_catalog_does_not_activate_academicae(
        self, cli_env: None, db: async_sessionmaker[AsyncSession]
    ) -> None:
        """Migrate → configure must leave Academicae inactive until activate-product.

        Starts from the post-`a1b2c3d4e5f6` state (fixed ID, inactive), applies the full
        launch catalog, and checks configuration landed without turning the product on.
        """
        async with db() as session:
            session.add(
                Product(
                    id=ACADEMICAE_ID,
                    slug="academicae",
                    display_name="Academicae",
                    is_active=False,
                )
            )
            await session.commit()

        assert await configure_products(Settings(), SHIPPED_CATALOG, out=io.StringIO()) == EXIT_OK

        async with db() as session:
            row = (
                await session.execute(select(Product).where(Product.id == ACADEMICAE_ID))
            ).scalar_one()

        assert row.is_active is False
        assert row.whatsapp_phone_number_id is not None
        assert row.google_calendar_id is not None
        assert row.audience_column_schema is not None
        assert row.demo_min_lead_days == 1
        assert row.demo_booking_horizon_days == -1

        async with db() as session:
            ai = (
                await session.execute(
                    select(AIConfiguration).where(
                        AIConfiguration.product_id == ACADEMICAE_ID,
                        AIConfiguration.segment_id.is_(None),
                    )
                )
            ).scalar_one()
        assert ai.qualification_mode == "ai_judgement"
        assert ai.system_prompt is not None

    async def test_a_second_run_changes_nothing(
        self, cli_env: None, db: async_sessionmaker[AsyncSession]
    ) -> None:
        await configure_products(Settings(), SHIPPED_CATALOG, out=io.StringIO())
        first = await product(db, "life-herba")

        out = io.StringIO()
        assert await configure_products(Settings(), SHIPPED_CATALOG, out=out) == EXIT_OK

        second = await product(db, "life-herba")
        assert first is not None and second is not None
        assert first.id == second.id
        assert "No changes" in out.getvalue()


class TestConfigurationIsSafeToApply:
    async def test_runtime_data_survives_a_reconfiguration(
        self, cli_env: None, db: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        await configure(tmp_path, ready_entry())
        owner = await product(db, "cfg-ready")
        assert owner is not None

        async with db() as session:
            session.add(Lead(phone_e164="+919876502701", full_name="Kept", product_id=owner.id))
            session.add(Audience(name="Kept", status=AudienceStatus.CONFIRMED, product_id=owner.id))
            await session.commit()

        code, _ = await configure(tmp_path, ready_entry(display_name="Renamed"))
        assert code == EXIT_OK

        async with db() as session:
            leads = await session.scalar(
                select(func.count()).select_from(Lead).where(Lead.product_id == owner.id)
            )
            audiences = await session.scalar(
                select(func.count()).select_from(Audience).where(Audience.product_id == owner.id)
            )
        renamed = await product(db, "cfg-ready")
        assert renamed is not None
        assert (renamed.id, renamed.display_name, leads, audiences) == (owner.id, "Renamed", 1, 1)

    async def test_configuration_never_changes_whether_a_product_is_active(
        self, cli_env: None, db: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        await configure(tmp_path, ready_entry())
        created = await product(db, "cfg-ready")
        assert created is not None and created.is_active is False

        async with db() as session:
            row = await session.get(Product, created.id)
            assert row is not None
            row.is_active = True
            await session.commit()

        await configure(tmp_path, ready_entry(display_name="Still active"))
        still = await product(db, "cfg-ready")
        assert still is not None and still.is_active is True

    async def test_an_absent_key_is_left_alone_and_null_clears(
        self, cli_env: None, db: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        await configure(tmp_path, ready_entry())

        await configure(tmp_path, {"slug": "cfg-ready", "display_name": "Cfg-Ready"})
        untouched = await product(db, "cfg-ready")
        assert (
            untouched is not None and untouched.google_calendar_id == "cfg-ready@calendar.example"
        )

        await configure(
            tmp_path, {"slug": "cfg-ready", "display_name": "Cfg-Ready", "google_calendar_id": None}
        )
        cleared = await product(db, "cfg-ready")
        assert cleared is not None and cleared.google_calendar_id is None

    async def test_an_id_belonging_to_another_slug_is_refused(
        self, cli_env: None, db: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """Slugs are permanent route identifiers. A catalog never renames one."""
        async with db() as session:
            session.add(Product(id=ACADEMICAE_ID, slug="product-1", display_name="Product 1"))
            await session.commit()

        code, output = await configure(
            tmp_path, {"slug": "academicae", "id": str(ACADEMICAE_ID), "display_name": "Academicae"}
        )

        assert code == EXIT_FAILED
        assert "never renames" in output
        assert await product(db, "academicae") is None

    async def test_a_number_held_by_another_product_is_refused(
        self, cli_env: None, db: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        await configure(tmp_path, ready_entry())

        code, output = await configure(
            tmp_path,
            {
                "slug": "cfg-other",
                "display_name": "Other",
                "whatsapp_phone_number_id": "cfg-ready-number",
            },
        )

        assert code == EXIT_FAILED
        assert "already configured on cfg-ready" in output
        # Refused whole: the second product was not created either.
        assert await product(db, "cfg-other") is None

    async def test_a_change_that_would_unready_an_active_product_is_refused(
        self, cli_env: None, db: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        await configure(tmp_path, ready_entry())
        assert (
            await activate_product(Settings(), "cfg-ready", accept_gaps=True, out=io.StringIO())
            == EXIT_OK
        )

        code, output = await configure(
            tmp_path, {"slug": "cfg-ready", "display_name": "Cfg-Ready", "google_calendar_id": None}
        )

        assert code == EXIT_FAILED
        assert "No Google Calendar ID" in output
        kept = await product(db, "cfg-ready")
        assert kept is not None and kept.google_calendar_id == "cfg-ready@calendar.example"

    async def test_a_dry_run_writes_nothing(
        self, cli_env: None, db: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        code, output = await configure(tmp_path, ready_entry(), dry_run=True)

        assert code == EXIT_OK
        assert "cfg-ready: created, inactive" in output
        assert await product(db, "cfg-ready") is None


class TestSegmentBehaviour:
    async def test_life_herba_shaped_overrides_are_sparse(
        self, cli_env: None, db: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """A product default plus one override stating only what differs. The segment with
        no override resolves exactly as the product does."""
        await configure(
            tmp_path,
            ready_entry(
                segments=[
                    {"slug": "pharmacies", "display_name": "Pharmacies"},
                    {"slug": "consumers", "display_name": "Consumers"},
                ],
                segment_ai_overrides={"pharmacies": {"system_prompt": "Pharmacy focus."}},
            ),
        )

        settings = Settings()
        defaults = ai_config_repo.platform_defaults_from_settings(settings)
        async with db() as session:
            owner = (
                await session.execute(select(Product).where(Product.slug == "cfg-ready"))
            ).scalar_one()
            segments = {
                s.slug: s.id
                for s in (
                    await session.execute(select(Segment).where(Segment.product_id == owner.id))
                ).scalars()
            }
            override = await ai_config_repo.get_segment_override(
                session, owner.id, segments["pharmacies"]
            )
            pharmacy = await ai_config_repo.resolve_for(
                session, product_id=owner.id, segment_id=segments["pharmacies"], defaults=defaults
            )
            consumer = await ai_config_repo.resolve_for(
                session, product_id=owner.id, segment_id=segments["consumers"], defaults=defaults
            )
            shared = await ai_config_repo.resolve_for(
                session, product_id=owner.id, segment_id=None, defaults=defaults
            )

        assert override is not None
        assert override.qualification_required_fields is None, "the override is sparse"
        assert pharmacy.system_prompt == "Pharmacy focus."
        assert pharmacy.qualification_required_fields == ("interested",)
        assert consumer == shared

    async def test_an_override_for_an_unknown_segment_is_refused(
        self, cli_env: None, db: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        code, output = await configure(
            tmp_path, ready_entry(segment_ai_overrides={"nobody": {"system_prompt": "x"}})
        )
        assert code == EXIT_FAILED
        assert "'nobody'" in output


class TestPolicyBelongsToEachProduct:
    async def test_two_configured_products_resolve_their_own_policy(
        self, cli_env: None, db: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        await configure(tmp_path, ready_entry(), ready_entry("cfg-other"))
        defaults = ai_config_repo.platform_defaults_from_settings(Settings())

        async with db() as session:
            ids = {
                p.slug: p.id
                for p in (
                    await session.execute(
                        select(Product).where(Product.slug.in_(["cfg-ready", "cfg-other"]))
                    )
                ).scalars()
            }
            first = await ai_config_repo.resolve_for(
                session, product_id=ids["cfg-ready"], segment_id=None, defaults=defaults
            )
            second = await ai_config_repo.resolve_for(
                session, product_id=ids["cfg-other"], segment_id=None, defaults=defaults
            )

        assert first.system_prompt == "The cfg-ready prompt."
        assert second.system_prompt == "The cfg-other prompt."

    async def test_a_product_without_policy_has_none_to_borrow(
        self, cli_env: None, db: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """No environment prompt, no other product's prompt — whether or not it is the only
        active product. The transitional sole-product rule is gone (Phase 27)."""
        await configure(tmp_path, ready_entry(), {"slug": "cfg-other", "display_name": "Other"})
        defaults = ai_config_repo.platform_defaults_from_settings(Settings())
        other = await product(db, "cfg-other")
        assert other is not None

        async with db() as session:
            with pytest.raises(AIConfigurationError):
                await ai_config_repo.resolve_for(
                    session, product_id=other.id, segment_id=None, defaults=defaults
                )


# ---------------------------------------------------------------- legacy import


class TestLegacyImport:
    async def _academicae(self, db: async_sessionmaker[AsyncSession]) -> None:
        async with db() as session:
            session.add(
                Product(
                    id=ACADEMICAE_ID, slug="academicae", display_name="Academicae", is_active=True
                )
            )
            await session.commit()

    async def test_copies_the_environment_into_the_named_product(
        self,
        cli_env: None,
        db: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await self._academicae(db)
        monkeypatch.setenv("AI_SYSTEM_PROMPT", "Academicae's configured prompt.")
        monkeypatch.setenv("QUALIFICATION_REQUIRED_FIELDS", "interested,institution")
        monkeypatch.setenv("AUDIENCE_COLUMN_SCHEMA", json.dumps(CONFIRMED_SCHEMA))
        monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "legacy-number")
        monkeypatch.setenv("WHATSAPP_BUSINESS_ACCOUNT_ID", "legacy-account")

        code = await import_legacy_config(
            Settings(), "academicae", environment=LegacyEnvironment(), out=io.StringIO()
        )
        assert code == EXIT_OK

        async with db() as session:
            row = await session.get(Product, ACADEMICAE_ID)
            policy = await ai_config_repo.get_product_default(session, ACADEMICAE_ID)
        assert row is not None and policy is not None
        assert row.audience_column_schema == CONFIRMED_SCHEMA
        assert (row.whatsapp_phone_number_id, row.whatsapp_business_account_id) == (
            "legacy-number",
            "legacy-account",
        )
        assert policy.system_prompt == "Academicae's configured prompt."
        assert policy.qualification_required_fields == ["interested", "institution"]
        # Activation state is not the import's business.
        assert row.is_active is True

    async def test_an_unset_environment_copies_the_defaults_it_ran_with(
        self, cli_env: None, db: async_sessionmaker[AsyncSession]
    ) -> None:
        await self._academicae(db)

        out = io.StringIO()
        code = await import_legacy_config(
            Settings(), "academicae", environment=LegacyEnvironment(), out=out
        )
        assert code == EXIT_OK
        assert "the built-in default the platform ran with" in out.getvalue()

        async with db() as session:
            row = await session.get(Product, ACADEMICAE_ID)
            policy = await ai_config_repo.get_product_default(session, ACADEMICAE_ID)
        assert row is not None and policy is not None
        assert policy.system_prompt == FORMER_DEFAULT_SYSTEM_PROMPT
        assert row.audience_column_schema == [dict(c) for c in DEVELOPMENT_PLACEHOLDER_COLUMNS]
        assert row.whatsapp_phone_number_id is None

    async def test_a_second_import_changes_nothing(
        self, cli_env: None, db: async_sessionmaker[AsyncSession]
    ) -> None:
        await self._academicae(db)
        await import_legacy_config(
            Settings(), "academicae", environment=LegacyEnvironment(), out=io.StringIO()
        )

        out = io.StringIO()
        code = await import_legacy_config(
            Settings(), "academicae", environment=LegacyEnvironment(), out=out
        )
        assert code == EXIT_OK
        assert "No changes" in out.getvalue()

    async def test_a_differing_value_is_a_conflict_unless_overwritten(
        self,
        cli_env: None,
        db: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await self._academicae(db)
        await import_legacy_config(
            Settings(), "academicae", environment=LegacyEnvironment(), out=io.StringIO()
        )
        monkeypatch.setenv("AI_SYSTEM_PROMPT", "A different prompt.")

        refused = io.StringIO()
        code = await import_legacy_config(
            Settings(), "academicae", environment=LegacyEnvironment(), out=refused
        )
        assert code == EXIT_FAILED
        assert "system_prompt" in refused.getvalue()

        async with db() as session:
            policy = await ai_config_repo.get_product_default(session, ACADEMICAE_ID)
        assert policy is not None and policy.system_prompt == FORMER_DEFAULT_SYSTEM_PROMPT

        code = await import_legacy_config(
            Settings(),
            "academicae",
            overwrite=True,
            environment=LegacyEnvironment(),
            out=io.StringIO(),
        )
        assert code == EXIT_OK
        async with db() as session:
            policy = await ai_config_repo.get_product_default(session, ACADEMICAE_ID)
        assert policy is not None and policy.system_prompt == "A different prompt."

    async def test_an_unknown_product_is_refused(
        self, cli_env: None, db: async_sessionmaker[AsyncSession]
    ) -> None:
        out = io.StringIO()
        code = await import_legacy_config(
            Settings(), "academicae", environment=LegacyEnvironment(), out=out
        )
        assert code == EXIT_FAILED
        assert "no product has the slug" in out.getvalue()


# ---------------------------------------------------------------- readiness and activation


class TestReadiness:
    async def test_a_fully_configured_inactive_product_is_ready_and_stays_inactive(
        self, cli_env: None, db: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        await configure(tmp_path, ready_entry())
        await give_campaign_template_and_knowledge(db, "cfg-ready")

        out = io.StringIO()
        code = await product_readiness(Settings(), ["cfg-ready"], as_json=True, out=out)
        report = json.loads(out.getvalue())

        assert code == EXIT_OK
        assert report["products"] == [
            {
                "slug": "cfg-ready",
                "display_name": "Cfg-Ready",
                "is_active": False,
                "verdict": "READY",
                "unsafe": False,
                "findings": [],
            }
        ]
        still = await product(db, "cfg-ready")
        assert still is not None and still.is_active is False

    async def test_the_shipped_products_are_not_ready_and_say_why(
        self, cli_env: None, db: async_sessionmaker[AsyncSession]
    ) -> None:
        await configure_products(Settings(), SHIPPED_CATALOG, out=io.StringIO())

        out = io.StringIO()
        code = await product_readiness(Settings(), ["sustainx"], as_json=True, out=out)
        (sustainx,) = json.loads(out.getvalue())["products"]

        # Inactive and not ready is the expected state while configuring: not a failure.
        assert code == EXIT_OK
        assert sustainx["verdict"] == "NOT READY"
        assert {f["code"] for f in sustainx["findings"] if f["severity"] == "BLOCKER"} == {
            "audience.schema_missing",
            "ai.policy_missing",
            "whatsapp.phone_number_missing",
            "whatsapp.business_account_missing",
            "calendar.missing",
        }

        out = io.StringIO()
        code = await product_readiness(Settings(), ["life-herba"], as_json=True, out=out)
        (life_herba,) = json.loads(out.getvalue())["products"]
        assert code == EXIT_OK
        assert life_herba["verdict"] == "NOT READY"
        life_herba_blockers = {
            f["code"] for f in life_herba["findings"] if f["severity"] == "BLOCKER"
        }
        assert life_herba_blockers == {
            "audience.schema_missing",
            "whatsapp.phone_number_missing",
            "whatsapp.business_account_missing",
        }
        assert "calendar.missing" not in life_herba_blockers
        assert "ai.policy_missing" not in life_herba_blockers

    async def test_an_active_product_that_is_not_ready_fails_the_check(
        self, cli_env: None, db: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        await configure(tmp_path, {"slug": "cfg-other", "display_name": "Other"})
        async with db() as session:
            row = (
                await session.execute(select(Product).where(Product.slug == "cfg-other"))
            ).scalar_one()
            row.is_active = True
            await session.commit()

        out = io.StringIO()
        code = await product_readiness(Settings(), ["cfg-other"], out=out)

        assert code == EXIT_FAILED
        assert "UNSAFE" in out.getvalue()

    async def test_a_number_shared_with_another_product_is_not_ready(
        self, cli_env: None, db: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """Written past the catalog's own refusal, as a hand edit to the database would be."""
        await configure(tmp_path, ready_entry(), {"slug": "cfg-other", "display_name": "Other"})
        async with db() as session:
            row = (
                await session.execute(select(Product).where(Product.slug == "cfg-other"))
            ).scalar_one()
            row.whatsapp_phone_number_id = "cfg-ready-number"
            await session.commit()

        out = io.StringIO()
        await product_readiness(Settings(), ["cfg-ready"], as_json=True, out=out)
        (ready,) = json.loads(out.getvalue())["products"]

        assert ready["verdict"] == "NOT READY"
        assert "whatsapp.phone_number_shared" in {f["code"] for f in ready["findings"]}

    async def test_crm_is_reported_deferred_while_several_products_exist(
        self, cli_env: None, db: async_sessionmaker[AsyncSession]
    ) -> None:
        await configure_products(Settings(), SHIPPED_CATALOG, out=io.StringIO())

        out = io.StringIO()
        await product_readiness(Settings(), out=out)

        assert "DEFERRED / DISABLED FOR MULTI-PRODUCT" in out.getvalue()


class TestActivation:
    async def test_a_product_that_is_not_ready_is_refused_and_stays_inactive(
        self, cli_env: None, db: async_sessionmaker[AsyncSession]
    ) -> None:
        await configure_products(Settings(), SHIPPED_CATALOG, out=io.StringIO())

        out = io.StringIO()
        code = await activate_product(Settings(), "life-herba", accept_gaps=True, out=out)

        assert code == EXIT_FAILED
        assert "is not ready" in out.getvalue()
        refused = await product(db, "life-herba")
        assert refused is not None and refused.is_active is False

    async def test_gaps_need_an_explicit_acknowledgement(
        self, cli_env: None, db: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """No template and no knowledge: it works, but activating it is a decision."""
        await configure(tmp_path, ready_entry())

        out = io.StringIO()
        assert await activate_product(Settings(), "cfg-ready", out=out) == EXIT_FAILED
        assert "--accept-gaps" in out.getvalue()
        waiting = await product(db, "cfg-ready")
        assert waiting is not None and waiting.is_active is False

        assert (
            await activate_product(Settings(), "cfg-ready", accept_gaps=True, out=io.StringIO())
            == EXIT_OK
        )
        active = await product(db, "cfg-ready")
        assert active is not None and active.is_active is True

    async def test_a_ready_product_activates_and_deactivates(
        self, cli_env: None, db: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        await configure(tmp_path, ready_entry())
        await give_campaign_template_and_knowledge(db, "cfg-ready")

        assert await activate_product(Settings(), "cfg-ready", out=io.StringIO()) == EXIT_OK
        assert await activate_product(Settings(), "cfg-ready", out=io.StringIO()) == EXIT_OK

        assert await deactivate_product(Settings(), "cfg-ready", out=io.StringIO()) == EXIT_OK
        stopped = await product(db, "cfg-ready")
        assert stopped is not None and stopped.is_active is False


class TestClearingJsonConfiguration:
    async def test_null_is_stored_as_sql_null_not_json_null(
        self, cli_env: None, db: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """Clearing a JSONB value must leave SQL NULL. A JSON `null` would read back as None
        yet count as configured to SQL — and the audience schema's check constraint rightly
        refuses it, which is how this was found."""
        await configure(
            tmp_path,
            ready_entry(demo_working_days=["MON", "TUE"]),
        )
        code, output = await configure(
            tmp_path,
            {
                "slug": "cfg-ready",
                "display_name": "Cfg-Ready",
                "audience_column_schema": None,
                "demo_working_days": None,
                "ai_configuration": {"qualification_required_fields": None},
            },
        )
        assert code == EXIT_OK, output

        async with db() as session:
            owner = (
                await session.execute(select(Product).where(Product.slug == "cfg-ready"))
            ).scalar_one()
            schema_is_null = await session.scalar(
                select(func.count())
                .select_from(Product)
                .where(Product.id == owner.id, Product.audience_column_schema.is_(None))
            )
            days_are_null = await session.scalar(
                select(func.count())
                .select_from(Product)
                .where(Product.id == owner.id, Product.demo_working_days.is_(None))
            )
            criteria_are_null = await session.scalar(
                select(func.count())
                .select_from(AIConfiguration)
                .where(
                    AIConfiguration.product_id == owner.id,
                    AIConfiguration.qualification_required_fields.is_(None),
                )
            )

        assert (schema_is_null, days_are_null, criteria_are_null) == (1, 1, 1)
