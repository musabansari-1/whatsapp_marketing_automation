"""Sender blocklist tests (operator block, not opt-out).

Blocking is abuse handling, not consent handling: a MANUAL suppression row stops all
platform activity for a number (no turns, no replies, no sends) without touching the
consent mirror, the CRM or acknowledgements. Those belong to the opt-out flow, and
the tests below assert the two never mix.
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.cli import EXIT_FAILED, EXIT_OK, block_sender, unblock_sender
from app.core.config import Settings, reset_settings_cache
from app.domain.enums import (
    LeadStatus,
    RecipientStatus,
    SuppressionReason,
)
from app.persistence import database
from app.persistence.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    Lead,
    WhatsAppSuppression,
    WhatsAppTemplate,
)
from app.persistence.repositories import suppressions as suppression_repo
from tests.integration.helpers import ensure_product_id, get_test_database_url

pytestmark = pytest.mark.integration

SPIKE = "+919876543211"


@pytest_asyncio.fixture
async def db(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def clear() -> None:
        async with factory() as session:
            await session.execute(delete(CampaignRecipient))
            await session.execute(delete(Campaign))
            await session.execute(delete(Audience))
            await session.execute(delete(WhatsAppTemplate))
            await session.execute(delete(Lead))
            await session.execute(delete(WhatsAppSuppression))
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


async def _reason(session: AsyncSession, phone: str) -> SuppressionReason | None:
    return await suppression_repo.suppression_reason(session, phone)


async def _row_count(session: AsyncSession) -> int:
    return len((await session.execute(select(WhatsAppSuppression))).scalars().all())


class TestBlockSender:
    async def test_blocks_a_number(
        self, cli_env: None, db: async_sessionmaker[AsyncSession]
    ) -> None:
        out = io.StringIO()
        assert await block_sender(Settings(), SPIKE, note="spam", out=out) == EXIT_OK
        assert SPIKE in out.getvalue()

        async with db() as session:
            row = (
                await session.execute(
                    select(WhatsAppSuppression).where(WhatsAppSuppression.phone_e164 == SPIKE)
                )
            ).scalar_one()
            assert row.reason is SuppressionReason.MANUAL
            assert row.notes == "spam"

    async def test_accepts_a_national_spelling(
        self, cli_env: None, db: async_sessionmaker[AsyncSession]
    ) -> None:
        assert await block_sender(Settings(), "9876543211", out=io.StringIO()) == EXIT_OK

        async with db() as session:
            assert await _reason(session, SPIKE) is SuppressionReason.MANUAL

    async def test_reblocking_is_idempotent(
        self, cli_env: None, db: async_sessionmaker[AsyncSession]
    ) -> None:
        assert await block_sender(Settings(), SPIKE, out=io.StringIO()) == EXIT_OK
        out = io.StringIO()
        assert await block_sender(Settings(), SPIKE, out=out) == EXIT_OK
        assert "already suppressed" in out.getvalue()

        async with db() as session:
            assert await _row_count(session) == 1

    async def test_refuses_garbage(
        self, cli_env: None, db: async_sessionmaker[AsyncSession]
    ) -> None:
        out = io.StringIO()
        assert await block_sender(Settings(), "not-a-number", out=out) == EXIT_FAILED
        assert "not a valid phone number" in out.getvalue()

        async with db() as session:
            assert await _row_count(session) == 0

    async def test_blocked_number_keeps_its_lead_mirror_untouched(
        self, cli_env: None, db: async_sessionmaker[AsyncSession]
    ) -> None:
        """A block is an operator safety action, not a consent event: the mirror
        stays consent-driven, and unblocking never has to repair it."""

        async def seed(session: AsyncSession) -> None:
            session.add(Lead(phone_e164=SPIKE, product_id=await ensure_product_id(session)))

        async with db() as session:
            await seed(session)
            await session.commit()

        assert await block_sender(Settings(), SPIKE, out=io.StringIO()) == EXIT_OK

        async def check(session: AsyncSession) -> tuple[bool, LeadStatus]:
            lead = (
                await session.execute(select(Lead).where(Lead.phone_e164 == SPIKE))
            ).scalar_one()
            return lead.opted_out, lead.status

        async with db() as session:
            assert await check(session) == (False, LeadStatus.NEW)

    async def test_block_skips_queued_sends(
        self, cli_env: None, db: async_sessionmaker[AsyncSession]
    ) -> None:
        """[D16] applies to blocks as much as to opt-outs: a prepared broadcast
        must not message a blocked number."""

        async def seed(session: AsyncSession) -> None:
            product_id = await ensure_product_id(session)
            session.add(Audience(name="A", product_id=product_id))
            session.add(
                WhatsAppTemplate(
                    name="t",
                    language="en",
                    provider_template_id="tpl-block",
                    product_id=product_id,
                    provider_status="APPROVED",
                    category="MARKETING",
                )
            )
            session.add(Lead(phone_e164=SPIKE, product_id=product_id))
            await session.flush()
            lead = (
                await session.execute(select(Lead).where(Lead.phone_e164 == SPIKE))
            ).scalar_one()
            campaign = Campaign(
                name="C",
                audience_id=(
                    await session.execute(select(Audience.id).where(Audience.name == "A"))
                ).scalar_one(),
                product_id=product_id,
                template_id=(
                    await session.execute(
                        select(WhatsAppTemplate.id).where(
                            WhatsAppTemplate.provider_template_id == "tpl-block"
                        )
                    )
                ).scalar_one(),
            )
            session.add(campaign)
            await session.flush()
            session.add(
                CampaignRecipient(
                    campaign_id=campaign.id,
                    lead_id=lead.id,
                    phone_e164=SPIKE,
                    status=RecipientStatus.QUEUED,
                )
            )

        async with db() as session:
            await seed(session)
            await session.commit()

        assert await block_sender(Settings(), SPIKE, out=io.StringIO()) == EXIT_OK

        async def status(session: AsyncSession) -> RecipientStatus:
            return (await session.execute(select(CampaignRecipient.status))).scalar_one()

        async with db() as session:
            assert await status(session) is RecipientStatus.SKIPPED


class TestUnblockSender:
    async def test_lifts_a_manual_block(
        self, cli_env: None, db: async_sessionmaker[AsyncSession]
    ) -> None:
        assert await block_sender(Settings(), SPIKE, out=io.StringIO()) == EXIT_OK
        out = io.StringIO()
        assert await unblock_sender(Settings(), SPIKE, out=out) == EXIT_OK
        assert "Unblocked" in out.getvalue()

        async with db() as session:
            assert await _row_count(session) == 0

    async def test_unblocking_an_absent_number_is_a_noop(
        self, cli_env: None, db: async_sessionmaker[AsyncSession]
    ) -> None:
        out = io.StringIO()
        assert await unblock_sender(Settings(), SPIKE, out=out) == EXIT_OK
        assert "not suppressed" in out.getvalue()

    async def test_never_lifts_an_opt_out(
        self, cli_env: None, db: async_sessionmaker[AsyncSession]
    ) -> None:
        """Consent is one-way: unblocking must not resurrect contactability someone
        refused, so an OPT_OUT row is refused and left intact."""

        async def seed(session: AsyncSession) -> None:
            session.add(
                WhatsAppSuppression(
                    phone_e164=SPIKE,
                    reason=SuppressionReason.OPT_OUT_KEYWORD,
                    source_message_id="wamid.x",
                )
            )

        async with db() as session:
            await seed(session)
            await session.commit()

        out = io.StringIO()
        assert await unblock_sender(Settings(), SPIKE, out=out) == EXIT_FAILED
        assert "never lifted here" in out.getvalue()

        async with db() as session:
            assert await _reason(session, SPIKE) is SuppressionReason.OPT_OUT_KEYWORD

    async def test_refuses_garbage(
        self, cli_env: None, db: async_sessionmaker[AsyncSession]
    ) -> None:
        assert await unblock_sender(Settings(), "zzz", out=io.StringIO()) == EXIT_FAILED
