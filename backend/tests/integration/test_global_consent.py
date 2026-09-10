"""Global consent across product-specific leads (Phase 20, [D42]).

`whatsapp_suppressions` is authoritative, global at the normalized phone level, and
carries no product. [D41] gave one human several lead rows, which made the mirror on
those rows hard to keep true: a STOP arrives through one product's conversation, and a
lead for another product may not even exist yet.

Two directions have to hold, and both are tested here:

    suppress now  -> every existing lead for that phone is corrected
    suppress now  -> every lead created later for that phone arrives suppressed

The mirror is never the check that decides whether a message may be sent. §7 checks the
table, at snapshot and again at send, and the last test here proves that still holds.

Products are created dynamically; nothing depends on Academicae's slug or UUID.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Iterator, Sequence

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.main import API_V1_PREFIX
from app.domain.enums import LeadStatus, RecipientStatus, SuppressionReason, TemplateCategory
from app.persistence.models import (
    Audience,
    AudienceContact,
    Campaign,
    CampaignRecipient,
    Lead,
    Product,
    Segment,
    WhatsAppSuppression,
    WhatsAppTemplate,
)
from app.persistence.repositories import suppressions as suppression_repo
from tests.integration.conftest import login
from tests.integration.helpers import run_db

pytestmark = pytest.mark.integration

XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PHONE = "9876544000"
E164 = f"+91{PHONE}"


def make_xlsx(rows: Sequence[Sequence[object]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    for row in rows:
        sheet.append(list(row))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _clean(seeded_users: dict[str, str]) -> Iterator[None]:
    async def clear(session: AsyncSession) -> None:
        await session.execute(delete(CampaignRecipient))
        await session.execute(delete(Campaign))
        await session.execute(delete(WhatsAppSuppression))
        await session.execute(delete(AudienceContact))
        await session.execute(delete(Lead))
        await session.execute(delete(Audience))
        await session.execute(delete(WhatsAppTemplate))
        await session.execute(delete(Segment))
        await session.execute(delete(Product).where(Product.slug.like("c-%")))

    run_db(clear)
    yield
    run_db(clear)


@pytest.fixture
def admin(
    client: TestClient, seeded_users: dict[str, str], seeded_product: uuid.UUID
) -> TestClient:
    login(client)
    return client


def make_product(slug: str) -> uuid.UUID:
    async def create(session: AsyncSession) -> uuid.UUID:
        product = Product(slug=slug, display_name=slug.upper())
        session.add(product)
        await session.flush()
        return product.id

    return run_db(create)


def make_lead(product_id: uuid.UUID | None, phone: str = E164) -> uuid.UUID:
    async def create(session: AsyncSession) -> uuid.UUID:
        lead = Lead(product_id=product_id, phone_e164=phone, full_name="Someone")
        session.add(lead)
        await session.flush()
        return lead.id

    return run_db(create)


def suppress(phone: str = E164) -> None:
    """Record the authoritative suppression and propagate, as the STOP path does."""

    async def run(session: AsyncSession) -> None:
        session.add(WhatsAppSuppression(phone_e164=phone, reason=SuppressionReason.OPT_OUT_KEYWORD))
        await session.flush()
        await suppression_repo.enforce_mirror_for(session, phone)

    run_db(run)


def mirrors(phone: str = E164) -> list[tuple[uuid.UUID | None, bool, LeadStatus]]:
    async def read(session: AsyncSession) -> list[tuple[uuid.UUID | None, bool, LeadStatus]]:
        result = await session.execute(
            select(Lead.product_id, Lead.opted_out, Lead.status).where(Lead.phone_e164 == phone)
        )
        return list(result.all())  # type: ignore[arg-type]

    return run_db(read)


def suppression_count(phone: str = E164) -> int:
    async def read(session: AsyncSession) -> int:
        result = await session.execute(
            select(WhatsAppSuppression).where(WhatsAppSuppression.phone_e164 == phone)
        )
        return len(list(result.scalars().all()))

    return run_db(read)


def upload_and_confirm(client: TestClient, name: str, product_id: uuid.UUID) -> str:
    response = client.post(
        f"{API_V1_PREFIX}/audiences/upload",
        files={
            "file": (
                f"{name}.xlsx",
                make_xlsx([["Name", "Phone"], ["Person", PHONE]]),
                XLSX_TYPE,
            )
        },
        data={"name": name, "product_id": str(product_id)},
    )
    assert response.status_code == 201, response.text
    audience_id = str(response.json()["audience"]["id"])
    client.post(f"{API_V1_PREFIX}/audiences/{audience_id}/confirm")
    return audience_id


class TestExistingLeadsAcrossProducts:
    def test_one_stop_suppresses_every_product(self) -> None:
        """Consent belongs to the person, not to the product that heard it."""
        first = make_product("c-first")
        second = make_product("c-second")
        make_lead(first)
        make_lead(second)

        suppress()

        assert suppression_count() == 1
        rows = mirrors()
        assert len(rows) == 2
        assert all(opted_out for _, opted_out, _ in rows)
        assert all(status is LeadStatus.OPTED_OUT for _, _, status in rows)

    def test_a_third_product_added_later_is_mirrored_as_well(self) -> None:
        """The mirror is by phone, so it does not depend on how many products exist.

        This replaced a test covering the Phase 19→23 window, when a lead could have no
        product yet and still needed suppressing. That row can no longer exist; what the
        test was really protecting — that the mirror follows the *person* — is what is
        asserted here instead.
        """
        first = make_product("c-first")
        second = make_product("c-second")
        third = make_product("c-third")
        for product in (first, second, third):
            make_lead(product)

        suppress()

        rows = mirrors()
        assert len(rows) == 3
        assert all(opted_out for _, opted_out, _ in rows)

    def test_an_invalid_lead_keeps_its_status_but_records_consent(self) -> None:
        """§10 makes INVALID terminal, and it records something opt-out does not: the
        number is unreachable.

        Consent is still recorded on the mirror. Overwriting the status would discard a
        finding to say something the suppression table already says better, and the
        transition table forbids it anyway.
        """
        product = make_product("c-first")
        lead_id = make_lead(product)

        async def invalidate(session: AsyncSession) -> None:
            lead = await session.get(Lead, lead_id)
            assert lead is not None
            lead.status = LeadStatus.INVALID

        run_db(invalidate)
        suppress()

        rows = mirrors()
        assert rows[0][1] is True, "consent must be recorded"
        assert rows[0][2] is LeadStatus.INVALID, "terminal status must survive"


class TestRepeatedStop:
    def test_a_second_stop_changes_nothing_and_raises_nothing(self) -> None:
        """Idempotent. `OPTED_OUT` is terminal, so routing this through
        `transition_lead` would refuse the second STOP — the reason for the documented
        exception in `enforce_mirror`."""
        product = make_product("c-first")
        make_lead(product)

        suppress()
        first = mirrors()

        # A repeat, as Meta redelivering the same message would produce.
        async def repeat(session: AsyncSession) -> int:
            return await suppression_repo.enforce_mirror_for(session, E164)

        corrected = run_db(repeat)

        assert corrected == 0, "nothing needed correcting the second time"
        assert suppression_count() == 1
        assert mirrors() == first

    def test_enforcing_for_an_unsuppressed_number_does_nothing(self) -> None:
        product = make_product("c-first")
        make_lead(product)

        async def enforce(session: AsyncSession) -> int:
            return await suppression_repo.enforce_mirror_for(session, E164)

        assert run_db(enforce) == 0
        assert mirrors()[0][1] is False


class TestLeadsCreatedAfterSuppression:
    def test_promotion_creates_an_already_suppressed_lead(self, admin: TestClient) -> None:
        """The case propagation at STOP time cannot cover.

        Nobody was a lead for this product when the number opted out. The lead is
        created later and must arrive suppressed rather than waiting for a campaign
        snapshot to notice.
        """
        suppress()
        assert mirrors() == []

        product = make_product("c-later")
        upload_and_confirm(admin, "later", product)

        rows = mirrors()
        assert len(rows) == 1
        assert rows[0][0] == product
        assert rows[0][1] is True, "a lead created after opt-out must arrive suppressed"
        assert rows[0][2] is LeadStatus.OPTED_OUT

    def test_a_second_upload_of_a_suppressed_lead_keeps_it_suppressed(
        self, admin: TestClient
    ) -> None:
        """Re-uploading someone must not make them contactable again.

        This replaced a test about adopting a suppressed *transitional* lead — a row with
        no product, which Phase 23 made impossible. The risk it guarded against is the
        same one here: promotion writes to an existing lead row, and the write must not
        undo consent.
        """
        product = make_product("c-later")
        upload_and_confirm(admin, "first", product)
        suppress()

        upload_and_confirm(admin, "again", product)

        rows = mirrors()
        assert len(rows) == 1, "the existing lead should be updated, not duplicated"
        assert rows[0][0] == product
        assert rows[0][1] is True
        assert rows[0][2] is LeadStatus.OPTED_OUT

    def test_several_products_added_after_suppression_are_all_suppressed(
        self, admin: TestClient
    ) -> None:
        suppress()

        first = make_product("c-first")
        second = make_product("c-second")
        upload_and_confirm(admin, "first-list", first)
        upload_and_confirm(admin, "second-list", second)

        rows = mirrors()
        assert len(rows) == 2
        assert {row[0] for row in rows} == {first, second}
        assert all(opted_out for _, opted_out, _ in rows)


class TestSendingStillChecksTheAuthoritativeTable:
    def test_campaign_snapshots_skip_a_suppressed_number(self, admin: TestClient) -> None:
        """The mirror is convenience; the table is the rule (§7, defence in depth).

        Checked by phone, not by lead — which is exactly why it survived per-product
        leads unchanged.
        """
        suppress()

        first = make_product("c-first")
        second = make_product("c-second")
        audiences = [
            upload_and_confirm(admin, "first-list", first),
            upload_and_confirm(admin, "second-list", second),
        ]

        template_id = self._template()
        for audience_id in audiences:
            response = admin.post(
                f"{API_V1_PREFIX}/campaigns",
                json={
                    "name": f"Campaign {audience_id[:8]}",
                    "audience_id": audience_id,
                    "template_id": template_id,
                },
            )
            assert response.status_code == 201, response.text
            campaign_id = uuid.UUID(response.json()["campaign"]["id"])

            async def statuses(session: AsyncSession, cid: uuid.UUID = campaign_id) -> list[str]:
                result = await session.execute(
                    select(CampaignRecipient.status).where(CampaignRecipient.campaign_id == cid)
                )
                return [status.value for status in result.scalars().all()]

            assert run_db(statuses) == [RecipientStatus.SKIPPED.value]

    def test_the_snapshot_would_skip_even_with_a_stale_mirror(self, admin: TestClient) -> None:
        """Proves the snapshot reads the table and not the mirror.

        The mirror is deliberately falsified here. Nothing in the application should
        ever produce this state — that is what Phase 20 prevents — but the check must
        not depend on it being true.
        """
        product = make_product("c-first")
        audience_id = upload_and_confirm(admin, "list", product)

        async def suppress_without_mirroring(session: AsyncSession) -> None:
            session.add(WhatsAppSuppression(phone_e164=E164))

        run_db(suppress_without_mirroring)
        assert mirrors()[0][1] is False, "mirror deliberately left stale"

        response = admin.post(
            f"{API_V1_PREFIX}/campaigns",
            json={
                "name": "Stale mirror",
                "audience_id": audience_id,
                "template_id": self._template(),
            },
        )
        campaign_id = uuid.UUID(response.json()["campaign"]["id"])

        async def statuses(session: AsyncSession) -> list[str]:
            result = await session.execute(
                select(CampaignRecipient.status).where(CampaignRecipient.campaign_id == campaign_id)
            )
            return [status.value for status in result.scalars().all()]

        assert run_db(statuses) == [RecipientStatus.SKIPPED.value]

    def test_consent_does_not_come_from_the_status_enum(self, admin: TestClient) -> None:
        """A suppressed lead whose status is *not* OPTED_OUT must still be skipped.

        `INVALID` is terminal and keeps its status under Phase 20's rule, so this lead is
        suppressed while `status != OPTED_OUT`. If any send path inferred consent from
        the enum, it would message someone who has opted out.

        Audited across `campaign_send` and the conversation turn: both check the
        suppression table. This pins that they keep doing so.
        """
        product = make_product("c-first")
        audience_id = upload_and_confirm(admin, "list", product)

        async def invalidate(session: AsyncSession) -> None:
            lead = (await session.execute(select(Lead).where(Lead.phone_e164 == E164))).scalar_one()
            lead.status = LeadStatus.INVALID

        run_db(invalidate)
        suppress()

        rows = mirrors()
        assert rows[0][1] is True
        assert rows[0][2] is LeadStatus.INVALID, "status is deliberately not OPTED_OUT here"

        response = admin.post(
            f"{API_V1_PREFIX}/campaigns",
            json={
                "name": "Invalid and suppressed",
                "audience_id": audience_id,
                "template_id": self._template(),
            },
        )
        campaign_id = uuid.UUID(response.json()["campaign"]["id"])

        async def statuses(session: AsyncSession) -> list[str]:
            result = await session.execute(
                select(CampaignRecipient.status).where(CampaignRecipient.campaign_id == campaign_id)
            )
            return [status.value for status in result.scalars().all()]

        assert run_db(statuses) == [RecipientStatus.SKIPPED.value]

    @staticmethod
    def _template() -> str:
        async def insert(session: AsyncSession) -> str:
            template = WhatsAppTemplate(
                name=f"consent_promo_{uuid.uuid4().hex[:6]}",
                language="en",
                provider_template_id=f"tpl-{uuid.uuid4()}",
                provider_status="APPROVED",
                category=TemplateCategory.MARKETING,
                variable_count=1,
                body_preview="Hello {{1}}.",
            )
            session.add(template)
            await session.flush()
            return str(template.id)

        return run_db(insert)


class TestSuppressionStaysGlobal:
    def test_the_table_has_no_product_column(self) -> None:
        """[D42]. Product-scoped consent is explicitly not implemented, and this is the
        cheapest possible guard against it arriving by accident."""
        assert "product_id" not in WhatsAppSuppression.__table__.columns
