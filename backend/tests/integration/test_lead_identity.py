"""Product-scoped lead identity (Phase 19, [D41]).

A lead is `(product, phone)`. The same human on one number is a separate lead in each
product, so their qualification in one cannot overwrite their qualification in another.
Within one product that number is exactly one lead, however many audiences and segments
it belongs to.

Phase 19 introduced this alongside a second, transitional rule, because the inbound path
could not resolve a product yet and [D45] forbids guessing one:

    product-known    UNIQUE (product_id, phone_e164) WHERE product_id IS NOT NULL
    transitional     UNIQUE (phone_e164)             WHERE product_id IS NULL

Phase 23 ended that. Inbound routes on the receiving number or discards the message, so
`leads.product_id` is NOT NULL, the transitional index is dropped, and one rule is left.

Products are created dynamically. Nothing here depends on Academicae's slug, name or
UUID, and SustainX and Life Herba are deliberately not seeded.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Iterator, Sequence

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.main import API_V1_PREFIX
from app.domain.enums import LeadStatus, RecipientStatus, TemplateCategory
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
from tests.integration.conftest import login
from tests.integration.helpers import run_db

pytestmark = pytest.mark.integration

XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

SHARED_PHONE = "9876522000"
OTHER_PHONE = "9876522001"


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
        await session.execute(delete(Product).where(Product.slug.like("t-%")))

    run_db(clear)
    yield
    run_db(clear)


@pytest.fixture
def admin(client: TestClient, seeded_users: dict[str, str]) -> TestClient:
    login(client)
    return client


def make_product(slug: str, *, active: bool = True) -> uuid.UUID:
    async def create(session: AsyncSession) -> uuid.UUID:
        product = Product(slug=slug, display_name=slug.upper(), is_active=active)
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


def make_lead(product_id: uuid.UUID | None, phone: str) -> uuid.UUID:
    async def create(session: AsyncSession) -> uuid.UUID:
        lead = Lead(product_id=product_id, phone_e164=phone, full_name="Someone")
        session.add(lead)
        await session.flush()
        return lead.id

    return run_db(create)


def leads_for(phone: str) -> list[tuple[uuid.UUID | None, LeadStatus]]:
    async def read(session: AsyncSession) -> list[tuple[uuid.UUID | None, LeadStatus]]:
        result = await session.execute(
            select(Lead.product_id, Lead.status).where(Lead.phone_e164 == phone)
        )
        return list(result.all())  # type: ignore[arg-type]

    return run_db(read)


def upload(
    client: TestClient,
    name: str,
    phones: Sequence[str],
    *,
    product_id: uuid.UUID | None = None,
    segment_id: uuid.UUID | None = None,
) -> tuple[int, str | None]:
    """Upload and confirm an audience. Returns the upload status and its id."""
    rows: list[Sequence[object]] = [["Name", "Phone"]]
    rows.extend([f"Person {phone}", phone] for phone in phones)

    data: dict[str, str] = {"name": name}
    if product_id is not None:
        data["product_id"] = str(product_id)
    if segment_id is not None:
        data["segment_id"] = str(segment_id)

    response = client.post(
        f"{API_V1_PREFIX}/audiences/upload",
        files={"file": (f"{name}.xlsx", make_xlsx(rows), XLSX_TYPE)},
        data=data,
    )
    if response.status_code != 201:
        return response.status_code, None

    audience_id = str(response.json()["audience"]["id"])
    client.post(f"{API_V1_PREFIX}/audiences/{audience_id}/confirm")
    return response.status_code, audience_id


class TestIdentityWithinAndAcrossProducts:
    def test_same_product_same_phone_is_one_lead(self, admin: TestClient) -> None:
        product = make_product("t-alpha")
        upload(admin, "first", [SHARED_PHONE], product_id=product)
        upload(admin, "second", [SHARED_PHONE], product_id=product)

        rows = leads_for(f"+91{SHARED_PHONE}")
        assert len(rows) == 1
        assert rows[0][0] == product

    def test_different_products_same_phone_are_separate_leads(self, admin: TestClient) -> None:
        """The point of [D41]. One human, two products, two independent lead rows."""
        alpha = make_product("t-alpha")
        beta = make_product("t-beta")

        upload(admin, "alpha-list", [SHARED_PHONE], product_id=alpha)
        upload(admin, "beta-list", [SHARED_PHONE], product_id=beta)

        rows = leads_for(f"+91{SHARED_PHONE}")
        assert len(rows) == 2
        assert {row[0] for row in rows} == {alpha, beta}

    def test_one_products_status_does_not_affect_the_other(self, admin: TestClient) -> None:
        """The reason a shared lead was rejected: status is a single column."""
        alpha = make_product("t-alpha")
        beta = make_product("t-beta")
        upload(admin, "alpha-list", [SHARED_PHONE], product_id=alpha)
        upload(admin, "beta-list", [SHARED_PHONE], product_id=beta)

        async def qualify_alpha(session: AsyncSession) -> None:
            lead = (
                await session.execute(
                    select(Lead).where(
                        Lead.phone_e164 == f"+91{SHARED_PHONE}", Lead.product_id == alpha
                    )
                )
            ).scalar_one()
            lead.status = LeadStatus.QUALIFIED

        run_db(qualify_alpha)

        by_product = dict(leads_for(f"+91{SHARED_PHONE}"))
        assert by_product[alpha] is LeadStatus.QUALIFIED
        assert by_product[beta] is LeadStatus.NEW

    def test_the_database_refuses_a_duplicate_within_one_product(self) -> None:
        product = make_product("t-alpha")
        make_lead(product, f"+91{SHARED_PHONE}")

        with pytest.raises(IntegrityError):
            make_lead(product, f"+91{SHARED_PHONE}")


class TestMembershipAcrossAudiencesAndSegments:
    def test_one_lead_through_several_audiences(self, admin: TestClient) -> None:
        product = make_product("t-alpha")
        upload(admin, "list-one", [SHARED_PHONE], product_id=product)
        upload(admin, "list-two", [SHARED_PHONE], product_id=product)

        assert len(leads_for(f"+91{SHARED_PHONE}")) == 1

        async def memberships(session: AsyncSession) -> int:
            result = await session.execute(
                select(AudienceContact).where(
                    AudienceContact.phone_e164 == f"+91{SHARED_PHONE}",
                    AudienceContact.lead_id.is_not(None),
                )
            )
            return len(list(result.scalars().all()))

        assert run_db(memberships) == 2

    def test_one_lead_through_several_segments(self, admin: TestClient) -> None:
        """[D48] made concrete: a business that is both a pharmacy and a wellness centre
        is one lead, targetable under both."""
        product = make_product("t-herba")
        pharmacies = make_segment(product, "pharmacies")
        wellness = make_segment(product, "wellness-centers")

        upload(admin, "pharmacy-list", [SHARED_PHONE], product_id=product, segment_id=pharmacies)
        upload(admin, "wellness-list", [SHARED_PHONE], product_id=product, segment_id=wellness)

        assert len(leads_for(f"+91{SHARED_PHONE}")) == 1

        async def segments_reached(session: AsyncSession) -> set[uuid.UUID]:
            result = await session.execute(
                select(Audience.segment_id)
                .join(AudienceContact, AudienceContact.audience_id == Audience.id)
                .where(AudienceContact.phone_e164 == f"+91{SHARED_PHONE}")
            )
            return {row[0] for row in result.all() if row[0] is not None}

        assert run_db(segments_reached) == {pharmacies, wellness}


class TestTheTransitionalLaneIsClosed:
    """Phase 19 allowed a lead with no product; Phase 23 does not.

    Kept as tests rather than deleted, because "a lead may have no product" was a real
    state that other code was written against — the adoption path in audience promotion,
    the turn handler's refusal to answer without context — and these are what say it has
    ended, so none of that is quietly resurrected.
    """

    def test_a_lead_cannot_be_created_without_a_product(self) -> None:
        with pytest.raises(IntegrityError):
            make_lead(None, f"+91{OTHER_PHONE}")

    def test_the_same_phone_is_still_one_lead_per_product(self) -> None:
        """The rule that survived, unchanged."""
        product = make_product("t-alpha")
        make_lead(product, f"+91{SHARED_PHONE}")

        with pytest.raises(IntegrityError):
            make_lead(product, f"+91{SHARED_PHONE}")

    def test_promotion_no_longer_needs_to_adopt_anything(self, admin: TestClient) -> None:
        """The case adoption existed for — message first, get uploaded second — now
        resolves on its own.

        The inbound lead already carries the product it was routed to, so an upload for
        that product finds it by `(product_id, phone_e164)` and updates it. There is no
        productless row to claim, which is why `_adopt_transitional_leads` is gone.
        """
        product = make_product("t-alpha")
        existing = make_lead(product, f"+91{SHARED_PHONE}")

        upload(admin, "later-list", [SHARED_PHONE], product_id=product)

        rows = leads_for(f"+91{SHARED_PHONE}")
        assert len(rows) == 1, "the existing lead must be updated, not duplicated"
        assert rows[0][0] == product

        async def same_row(session: AsyncSession) -> bool:
            lead = await session.get(Lead, existing)
            return lead is not None

        assert run_db(same_row), "the original row must survive, keeping its conversations"


class TestGlobalSuppressionSurvives:
    def test_one_suppression_stops_every_product(self, admin: TestClient) -> None:
        """[D42]. Opt-out is global at the phone level, and per-product leads must not
        weaken it.

        The check is by phone, not by lead, which is exactly why it still holds.
        """
        alpha = make_product("t-alpha")
        beta = make_product("t-beta")

        _, alpha_audience = upload(admin, "alpha-list", [SHARED_PHONE], product_id=alpha)
        _, beta_audience = upload(admin, "beta-list", [SHARED_PHONE], product_id=beta)
        assert len(leads_for(f"+91{SHARED_PHONE}")) == 2

        async def suppress(session: AsyncSession) -> None:
            session.add(WhatsAppSuppression(phone_e164=f"+91{SHARED_PHONE}"))

        run_db(suppress)

        template_id = self._template()
        for audience_id in (alpha_audience, beta_audience):
            assert audience_id is not None
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

    @staticmethod
    def _template() -> str:
        async def insert(session: AsyncSession) -> str:
            template = WhatsAppTemplate(
                name="identity_promo",
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


class TestCampaignScope:
    def test_a_campaign_takes_its_product_from_its_audience(self, admin: TestClient) -> None:
        product = make_product("t-alpha")
        segment = make_segment(product, "government")
        _, audience_id = upload(
            admin, "gov-list", [SHARED_PHONE], product_id=product, segment_id=segment
        )
        assert audience_id is not None

        response = admin.post(
            f"{API_V1_PREFIX}/campaigns",
            json={
                "name": "Derived",
                "audience_id": audience_id,
                "template_id": TestGlobalSuppressionSurvives._template(),
            },
        )
        assert response.status_code == 201, response.text
        campaign_id = uuid.UUID(response.json()["campaign"]["id"])

        async def read(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID | None]:
            campaign = await session.get(Campaign, campaign_id)
            assert campaign is not None
            return campaign.product_id, campaign.segment_id

        assert run_db(read) == (product, segment)
