"""The dashboard API, one product at a time (§28, Phase 26).

Every product-scoped route lives under `/api/v1/p/{product_slug}`. These tests exist to
catch the failures that would be silent in a dashboard: a list that quietly includes
another product's rows, a record reachable from the wrong product's address, a campaign
that borrows another product's audience or template, knowledge created with no owner, a
count that adds two products together, and an inactive product still starting work.

Products and segments are created here; nothing depends on any real product's identity.
"""

from __future__ import annotations

import io
import itertools
import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.main import API_V1_PREFIX
from app.domain.enums import (
    AudienceStatus,
    BookingStatus,
    CampaignStatus,
    ContactValidationStatus,
    ConversationStatus,
    LeadStatus,
    NotificationType,
    RecipientStatus,
    TemplateCategory,
)
from app.persistence.models import (
    Audience,
    AudienceContact,
    Booking,
    Campaign,
    CampaignRecipient,
    Conversation,
    ConversationMessage,
    KnowledgeBase,
    KnowledgeDocument,
    Lead,
    Message,
    Notification,
    Product,
    Segment,
    User,
    WhatsAppSuppression,
    WhatsAppTemplate,
)
from tests.integration.conftest import ADMIN_EMAIL, SALES_EMAIL, login
from tests.integration.helpers import TEST_AUDIENCE_SCHEMA, run_db, scoped_prefix

pytestmark = pytest.mark.integration

XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
START = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)
#: A distinct hour per seeded booking: one product confirms an hour once ([D43]).
_HOURS = itertools.count()


@pytest.fixture(autouse=True)
def _clean(seeded_users: dict[str, str]) -> Iterator[None]:
    async def clear(session: AsyncSession) -> None:
        for model in (
            Notification,
            Booking,
            ConversationMessage,
            Message,
            Conversation,
            CampaignRecipient,
            Campaign,
            AudienceContact,
            WhatsAppSuppression,
            Lead,
            Audience,
            WhatsAppTemplate,
            KnowledgeDocument,
            KnowledgeBase,
        ):
            await session.execute(delete(model))
        products = select(Product.id).where(Product.slug.like("s-%"))
        await session.execute(delete(Segment).where(Segment.product_id.in_(products)))
        await session.execute(delete(Product).where(Product.slug.like("s-%")))

    run_db(clear)
    yield
    run_db(clear)
    from app.api.deps import reset_queue_provider

    reset_queue_provider()


@pytest.fixture
def admin(client: TestClient, seeded_users: dict[str, str]) -> TestClient:
    login(client)
    return client


# ---------------------------------------------------------------- seeding


def make_product(slug: str, *, active: bool = True) -> uuid.UUID:
    async def create(session: AsyncSession) -> uuid.UUID:
        product = Product(
            slug=slug,
            display_name=slug.upper(),
            is_active=active,
            audience_column_schema=[dict(column) for column in TEST_AUDIENCE_SCHEMA],
        )
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


def make_xlsx(rows: Sequence[Sequence[object]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    for row in rows:
        sheet.append(list(row))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


class World:
    """One product's worth of rows, seeded directly: every kind of product-scoped record."""

    def __init__(self, product_id: uuid.UUID, *, phone: str, segment_id: uuid.UUID | None = None):
        self.product_id = product_id
        ids = run_db(lambda session: self._seed(session, phone, segment_id))
        (
            self.audience,
            self.template,
            self.campaign,
            self.lead,
            self.conversation,
            self.booking,
            self.knowledge_base,
        ) = ids

    async def _seed(
        self, session: AsyncSession, phone: str, segment_id: uuid.UUID | None
    ) -> tuple[uuid.UUID, ...]:
        audience = Audience(
            name=f"audience-{phone}",
            status=AudienceStatus.CONFIRMED,
            product_id=self.product_id,
            segment_id=segment_id,
        )
        template = WhatsAppTemplate(
            name=f"tpl_{uuid.uuid4().hex[:8]}",
            language="en",
            product_id=self.product_id,
            provider_template_id=f"tpl-{uuid.uuid4()}",
            provider_status="APPROVED",
            category=TemplateCategory.MARKETING,
            variable_count=1,
        )
        lead = Lead(product_id=self.product_id, phone_e164=phone, full_name="Asha Rao")
        session.add_all([audience, template, lead])
        await session.flush()

        session.add(
            AudienceContact(
                audience_id=audience.id,
                row_number=1,
                phone_e164=phone,
                lead_id=lead.id,
                validation_status=ContactValidationStatus.VALID,
                raw_row={},
            )
        )
        campaign = Campaign(
            name=f"campaign-{phone}",
            audience_id=audience.id,
            product_id=self.product_id,
            segment_id=segment_id,
            template_id=template.id,
            status=CampaignStatus.READY,
        )
        conversation = Conversation(
            lead_id=lead.id,
            product_id=self.product_id,
            segment_id=segment_id,
            status=ConversationStatus.ACTIVE,
        )
        knowledge_base = KnowledgeBase(
            name=f"kb-{phone}", product_id=self.product_id, segment_id=None
        )
        session.add_all([campaign, conversation, knowledge_base])
        await session.flush()

        session.add(
            CampaignRecipient(
                campaign_id=campaign.id,
                lead_id=lead.id,
                phone_e164=phone,
                status=RecipientStatus.PENDING,
            )
        )
        start = START + timedelta(hours=next(_HOURS))
        booking = Booking(
            product_id=self.product_id,
            lead_id=lead.id,
            conversation_id=conversation.id,
            calendar_event_id=f"demo-{uuid.uuid4()}",
            start_time=start,
            end_time=start + timedelta(minutes=30),
            timezone="Asia/Kolkata",
            status=BookingStatus.CONFIRMED,
        )
        session.add(booking)
        await session.flush()

        return (
            audience.id,
            template.id,
            campaign.id,
            lead.id,
            conversation.id,
            booking.id,
            knowledge_base.id,
        )


def ids_of(body: Any) -> set[str]:
    items = body["items"] if isinstance(body, dict) else body
    return {item["id"] for item in items}


def user_id(email: str) -> uuid.UUID:
    async def read(session: AsyncSession) -> uuid.UUID:
        return (await session.execute(select(User.id).where(User.email == email))).scalar_one()

    return run_db(read)


# ---------------------------------------------------------------- products


class TestProducts:
    def test_the_switcher_lists_products_from_the_database(self, admin: TestClient) -> None:
        make_product("s-first")
        make_product("s-retired", active=False)

        listed = {p["slug"]: p["is_active"] for p in admin.get(f"{API_V1_PREFIX}/products").json()}

        assert listed["s-first"] is True
        # Returned so its history stays reachable; the dashboard labels it.
        assert listed["s-retired"] is False

    def test_a_product_describes_its_segments(self, admin: TestClient) -> None:
        product = make_product("s-segmented")
        make_segment(product, "government")
        make_segment(product, "industry")

        body = admin.get(f"{API_V1_PREFIX}/p/s-segmented").json()

        assert body["slug"] == "s-segmented"
        assert {s["slug"] for s in body["segments"]} == {"government", "industry"}
        assert body["has_calendar"] is False
        assert body["has_meeting_url"] is False

    def test_a_product_with_no_segments_works_normally(self, admin: TestClient) -> None:
        make_product("s-plain")

        assert admin.get(f"{API_V1_PREFIX}/p/s-plain").json()["segments"] == []
        assert admin.get(f"{API_V1_PREFIX}/p/s-plain/audiences").json() == []
        assert admin.get(f"{API_V1_PREFIX}/p/s-plain/leads").json()["total"] == 0

    def test_an_unknown_product_is_404(self, admin: TestClient) -> None:
        assert admin.get(f"{API_V1_PREFIX}/p/s-nowhere/campaigns").status_code == 404

    def test_authentication_is_decided_before_the_product(self, client: TestClient) -> None:
        """An anonymous caller learns nothing about which products exist."""
        make_product("s-private")

        assert client.get(f"{API_V1_PREFIX}/p/s-private/leads").status_code == 401
        assert client.get(f"{API_V1_PREFIX}/p/s-nowhere/leads").status_code == 401


# ---------------------------------------------------------------- isolation


class TestListsNeverCrossProducts:
    def test_every_list_holds_only_its_own_products_rows(self, admin: TestClient) -> None:
        alpha = World(make_product("s-alpha"), phone="+919876501001")
        beta = World(make_product("s-beta"), phone="+919876501002")

        for world, other in ((alpha, beta), (beta, alpha)):
            base = scoped_prefix(world.product_id)
            expected = {
                "audiences": world.audience,
                "campaigns": world.campaign,
                "whatsapp-templates": world.template,
                "leads": world.lead,
                "conversations": world.conversation,
                "bookings": world.booking,
                "knowledge-bases": world.knowledge_base,
            }
            for resource, own in expected.items():
                response = admin.get(f"{base}/{resource}")
                assert response.status_code == 200, (resource, response.text)
                assert ids_of(response.json()) == {str(own)}, (
                    f"{resource} of one product contains another's rows"
                )
            assert str(other.lead) not in ids_of(admin.get(f"{base}/leads").json())


class TestForeignRecordsAre404:
    def test_another_products_records_read_as_missing(self, admin: TestClient) -> None:
        """404, never 403: the same answer as a record that does not exist, so one
        product's contents cannot be probed from another's address."""
        make_product("s-alpha")
        beta = World(make_product("s-beta"), phone="+919876501012")
        alpha_base = f"{API_V1_PREFIX}/p/s-alpha"

        reads = [
            f"/audiences/{beta.audience}",
            f"/audiences/{beta.audience}/contacts",
            f"/campaigns/{beta.campaign}",
            f"/campaigns/{beta.campaign}/recipients",
            f"/leads/{beta.lead}",
            f"/leads/{beta.lead}/conversation",
            f"/conversations/{beta.conversation}",
            f"/bookings/{beta.booking}",
            f"/knowledge-bases/{beta.knowledge_base}",
            f"/knowledge-bases/{beta.knowledge_base}/documents",
        ]
        for path in reads:
            assert admin.get(alpha_base + path).status_code == 404, path

        writes: list[tuple[str, str, dict[str, Any]]] = [
            ("patch", f"/leads/{beta.lead}", {"json": {"city": "Pune"}}),
            ("post", f"/audiences/{beta.audience}/confirm", {}),
            ("post", f"/campaigns/{beta.campaign}/send", {}),
            ("post", f"/campaigns/{beta.campaign}/cancel", {}),
            (
                "post",
                f"/knowledge-bases/{beta.knowledge_base}/documents",
                {"files": {"file": ("a.txt", b"text", "text/plain")}},
            ),
        ]
        for method, path, kwargs in writes:
            assert getattr(admin, method)(alpha_base + path, **kwargs).status_code == 404, path

        # And nothing in the other product changed.
        assert (
            admin.get(f"{API_V1_PREFIX}/p/s-beta/campaigns/{beta.campaign}").json()["campaign"][
                "status"
            ]
            == "READY"
        )

    def test_the_bookings_lead_filter_cannot_reach_across(self, admin: TestClient) -> None:
        make_product("s-alpha")
        beta = World(make_product("s-beta"), phone="+919876501013")

        body = admin.get(f"{API_V1_PREFIX}/p/s-alpha/bookings", params={"lead_id": str(beta.lead)})

        assert body.json()["total"] == 0


class TestCampaignsCannotBorrow:
    def test_a_foreign_audience_is_404(self, admin: TestClient) -> None:
        alpha = World(make_product("s-alpha"), phone="+919876501021")
        beta = World(make_product("s-beta"), phone="+919876501022")

        response = admin.post(
            f"{API_V1_PREFIX}/p/s-alpha/campaigns",
            json={
                "name": "Borrowed",
                "audience_id": str(beta.audience),
                "template_id": str(alpha.template),
            },
        )
        assert response.status_code == 404

    def test_a_foreign_template_is_refused(self, admin: TestClient) -> None:
        alpha = World(make_product("s-alpha"), phone="+919876501023")
        beta = World(make_product("s-beta"), phone="+919876501024")

        response = admin.post(
            f"{API_V1_PREFIX}/p/s-alpha/campaigns",
            json={
                "name": "Borrowed",
                "audience_id": str(alpha.audience),
                "template_id": str(beta.template),
            },
        )
        assert response.status_code == 422
        assert "different product" in response.text

    def test_an_unassigned_template_is_refused(self, admin: TestClient) -> None:
        alpha = World(make_product("s-alpha"), phone="+919876501025")

        async def unassign(session: AsyncSession) -> None:
            template = await session.get(WhatsAppTemplate, alpha.template)
            assert template is not None
            template.product_id = None

        run_db(unassign)

        response = admin.post(
            f"{API_V1_PREFIX}/p/s-alpha/campaigns",
            json={
                "name": "Unowned",
                "audience_id": str(alpha.audience),
                "template_id": str(alpha.template),
            },
        )
        assert response.status_code == 422
        assert "not been assigned" in response.text


# ---------------------------------------------------------------- templates


def unassigned_template(name: str = "welcome") -> uuid.UUID:
    async def create(session: AsyncSession) -> uuid.UUID:
        template = WhatsAppTemplate(
            name=name,
            language="en",
            product_id=None,
            provider_template_id=f"tpl-{uuid.uuid4()}",
            provider_status="APPROVED",
            category=TemplateCategory.MARKETING,
            variable_count=0,
        )
        session.add(template)
        await session.flush()
        return template.id

    return run_db(create)


class TestTemplateAssignment:
    def test_an_admin_assigns_a_template_and_it_appears_for_that_product_only(
        self, admin: TestClient
    ) -> None:
        alpha = make_product("s-alpha")
        make_product("s-beta")
        template = unassigned_template()

        unassigned = admin.get(f"{API_V1_PREFIX}/whatsapp-templates/unassigned").json()
        assert str(template) in ids_of(unassigned)

        response = admin.post(f"{API_V1_PREFIX}/p/s-alpha/whatsapp-templates/{template}/assign")

        assert response.status_code == 200, response.text
        assert response.json()["product_id"] == str(alpha)
        assert response.json()["usable_for_campaigns"] is True
        assert str(template) in ids_of(
            admin.get(f"{API_V1_PREFIX}/p/s-alpha/whatsapp-templates").json()
        )
        assert str(template) not in ids_of(
            admin.get(f"{API_V1_PREFIX}/p/s-beta/whatsapp-templates").json()
        )
        assert str(template) not in ids_of(
            admin.get(f"{API_V1_PREFIX}/whatsapp-templates/unassigned").json()
        )

    def test_a_salesperson_cannot_assign(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        make_product("s-alpha")
        template = unassigned_template()
        login(client, email=SALES_EMAIL)

        response = client.post(f"{API_V1_PREFIX}/p/s-alpha/whatsapp-templates/{template}/assign")

        assert response.status_code == 403
        assert str(template) in ids_of(
            client.get(f"{API_V1_PREFIX}/whatsapp-templates/unassigned").json()
        )

    def test_an_owned_template_is_not_moved_by_assignment(self, admin: TestClient) -> None:
        alpha = make_product("s-alpha")
        make_product("s-beta")
        template = unassigned_template()
        admin.post(f"{API_V1_PREFIX}/p/s-alpha/whatsapp-templates/{template}/assign")

        response = admin.post(f"{API_V1_PREFIX}/p/s-beta/whatsapp-templates/{template}/assign")

        assert response.status_code == 409
        owner = run_db(lambda session: session.get(WhatsAppTemplate, template))
        assert owner is not None and owner.product_id == alpha

    def test_an_unknown_template_is_404(self, admin: TestClient) -> None:
        make_product("s-alpha")
        assert (
            admin.post(
                f"{API_V1_PREFIX}/p/s-alpha/whatsapp-templates/{uuid.uuid4()}/assign"
            ).status_code
            == 404
        )


# ---------------------------------------------------------------- knowledge


class TestKnowledgeBelongsToAProduct:
    def test_there_is_no_productless_knowledge_route(self, admin: TestClient) -> None:
        payload = {"name": "Orphan", "description": None}
        assert admin.post(f"{API_V1_PREFIX}/knowledge-bases", json=payload).status_code == 404
        assert admin.get(f"{API_V1_PREFIX}/knowledge-bases").status_code == 404

    def test_a_knowledge_base_is_owned_by_the_product_in_the_address(
        self, admin: TestClient
    ) -> None:
        alpha = make_product("s-alpha")
        make_product("s-beta")

        created = admin.post(
            f"{API_V1_PREFIX}/p/s-alpha/knowledge-bases", json={"name": "FAQs", "description": None}
        )

        assert created.status_code == 201, created.text
        assert created.json()["product_id"] == str(alpha)
        assert created.json()["segment_id"] is None
        assert admin.get(f"{API_V1_PREFIX}/p/s-beta/knowledge-bases").json() == []

    def test_names_are_unique_within_a_product_only(self, admin: TestClient) -> None:
        make_product("s-alpha")
        make_product("s-beta")
        payload = {"name": "FAQs", "description": None}

        assert (
            admin.post(f"{API_V1_PREFIX}/p/s-alpha/knowledge-bases", json=payload).status_code
            == 201
        )
        assert (
            admin.post(f"{API_V1_PREFIX}/p/s-beta/knowledge-bases", json=payload).status_code == 201
        )
        again = admin.post(
            f"{API_V1_PREFIX}/p/s-alpha/knowledge-bases", json={"name": "faqs", "description": None}
        )
        assert again.status_code == 409

    def test_a_segment_must_belong_to_the_product(self, admin: TestClient) -> None:
        alpha = make_product("s-alpha")
        beta = make_product("s-beta")
        own = make_segment(alpha, "pharmacies")
        foreign = make_segment(beta, "pharmacies")

        refused = admin.post(
            f"{API_V1_PREFIX}/p/s-alpha/knowledge-bases",
            json={"name": "Foreign", "description": None, "segment_id": str(foreign)},
        )
        accepted = admin.post(
            f"{API_V1_PREFIX}/p/s-alpha/knowledge-bases",
            json={"name": "Own", "description": None, "segment_id": str(own)},
        )

        assert refused.status_code == 404
        assert accepted.status_code == 201
        assert accepted.json()["segment_id"] == str(own)


# ---------------------------------------------------------------- notifications


def notify(email: str, product_id: uuid.UUID | None, title: str) -> None:
    async def create(session: AsyncSession) -> None:
        recipient = (await session.execute(select(User).where(User.email == email))).scalar_one()
        session.add(
            Notification(
                user_id=recipient.id,
                product_id=product_id,
                type=NotificationType.DEMO_BOOKED,
                title=title,
                message="A demo was booked.",
                related_entity_type="booking",
                related_entity_id=uuid.uuid4(),
            )
        )

    run_db(create)


class TestNotifications:
    def test_each_notification_names_its_product(self, admin: TestClient) -> None:
        alpha = make_product("s-alpha")
        notify(ADMIN_EMAIL, alpha, "Demo booked — S-ALPHA")

        [item] = admin.get(f"{API_V1_PREFIX}/notifications").json()["items"]

        assert item["product_id"] == str(alpha)
        assert item["product"] == {"id": str(alpha), "slug": "s-alpha", "display_name": "S-ALPHA"}

    def test_the_product_filter_narrows_the_page_not_the_badge(self, admin: TestClient) -> None:
        alpha = make_product("s-alpha")
        beta = make_product("s-beta")
        notify(ADMIN_EMAIL, alpha, "Alpha demo")
        notify(ADMIN_EMAIL, beta, "Beta demo")

        body = admin.get(f"{API_V1_PREFIX}/notifications", params={"product": "s-alpha"}).json()

        assert [item["title"] for item in body["items"]] == ["Alpha demo"]
        assert body["unread"] == 2, "the badge counts everything waiting for this person"

    def test_an_unknown_product_filter_is_404(self, admin: TestClient) -> None:
        response = admin.get(f"{API_V1_PREFIX}/notifications", params={"product": "s-nowhere"})
        assert response.status_code == 404


# ---------------------------------------------------------------- analytics


def overview(client: TestClient, base: str, **params: str) -> dict[str, Any]:
    response = client.get(f"{base}/analytics/overview", params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


class TestAnalytics:
    def test_counts_never_cross_products_and_combine_explicitly(self, admin: TestClient) -> None:
        World(make_product("s-alpha"), phone="+919876501031")
        World(make_product("s-beta"), phone="+919876501032")
        World(run_db(lambda s: _product_id(s, "s-beta")), phone="+919876501033")

        alpha = overview(admin, f"{API_V1_PREFIX}/p/s-alpha")
        beta = overview(admin, f"{API_V1_PREFIX}/p/s-beta")
        combined = overview(admin, API_V1_PREFIX)

        assert (alpha["total_leads"], beta["total_leads"]) == (1, 2)
        assert (alpha["campaigns"], beta["campaigns"]) == (1, 2)
        assert (alpha["demos_booked"], beta["demos_booked"]) == (1, 2)
        assert combined["total_leads"] == alpha["total_leads"] + beta["total_leads"]
        assert combined["demos_booked"] == alpha["demos_booked"] + beta["demos_booked"]

    def test_a_segment_narrows_within_its_product(self, admin: TestClient) -> None:
        product = make_product("s-herba")
        pharmacies = make_segment(product, "pharmacies")
        World(product, phone="+919876501041", segment_id=pharmacies)
        World(product, phone="+919876501042")

        whole = overview(admin, f"{API_V1_PREFIX}/p/s-herba")
        segment = overview(admin, f"{API_V1_PREFIX}/p/s-herba", segment="pharmacies")

        assert whole["total_leads"] == 2
        assert segment["total_leads"] == 1
        assert segment["campaigns"] == 1
        assert segment["demos_booked"] == 1

    def test_another_products_segment_is_404(self, admin: TestClient) -> None:
        make_product("s-alpha")
        beta = make_product("s-beta")
        make_segment(beta, "industry")

        response = admin.get(
            f"{API_V1_PREFIX}/p/s-alpha/analytics/overview", params={"segment": "industry"}
        )
        assert response.status_code == 404


async def _product_id(session: AsyncSession, slug: str) -> uuid.UUID:
    return (await session.execute(select(Product.id).where(Product.slug == slug))).scalar_one()


# ---------------------------------------------------------------- one person, two products


class TestOnePersonInTwoProducts:
    def test_the_same_phone_is_a_separate_lead_in_each_product(self, admin: TestClient) -> None:
        """[D41]: never merged by the dashboard because the number matches."""
        phone = "+919876501051"
        alpha = World(make_product("s-alpha"), phone=phone)
        beta = World(make_product("s-beta"), phone=phone)

        async def qualify(session: AsyncSession) -> None:
            lead = await session.get(Lead, beta.lead)
            assert lead is not None
            lead.status = LeadStatus.QUALIFIED

        run_db(qualify)

        in_alpha = admin.get(f"{API_V1_PREFIX}/p/s-alpha/leads", params={"search": phone}).json()
        in_beta = admin.get(f"{API_V1_PREFIX}/p/s-beta/leads", params={"search": phone}).json()

        assert [(i["id"], i["status"]) for i in in_alpha["items"]] == [(str(alpha.lead), "NEW")]
        assert [(i["id"], i["status"]) for i in in_beta["items"]] == [(str(beta.lead), "QUALIFIED")]


# ---------------------------------------------------------------- segment filters


class TestSegmentFilters:
    def test_filters_narrow_within_a_product(self, admin: TestClient) -> None:
        product = make_product("s-sustain")
        government = make_segment(product, "government")
        industry = make_segment(product, "industry")
        gov = World(product, phone="+919876501061", segment_id=government)
        World(product, phone="+919876501062", segment_id=industry)
        World(product, phone="+919876501063")

        base = f"{API_V1_PREFIX}/p/s-sustain"
        filtered = {"segment": "government"}

        assert ids_of(admin.get(f"{base}/audiences", params=filtered).json()) == {str(gov.audience)}
        assert ids_of(admin.get(f"{base}/campaigns", params=filtered).json()) == {str(gov.campaign)}
        assert ids_of(admin.get(f"{base}/conversations", params=filtered).json()) == {
            str(gov.conversation)
        }
        assert ids_of(admin.get(f"{base}/bookings", params=filtered).json()) == {str(gov.booking)}
        assert ids_of(admin.get(f"{base}/leads", params=filtered).json()) == {str(gov.lead)}

        # "All segments" includes rows with no segment at all.
        assert len(admin.get(f"{base}/audiences").json()) == 3
        assert admin.get(f"{base}/leads").json()["total"] == 3

    def test_an_unknown_segment_is_404(self, admin: TestClient) -> None:
        make_product("s-plain")
        response = admin.get(f"{API_V1_PREFIX}/p/s-plain/leads", params={"segment": "nowhere"})
        assert response.status_code == 404

    def test_an_audience_cannot_take_another_products_segment(self, admin: TestClient) -> None:
        make_product("s-alpha")
        foreign = make_segment(make_product("s-beta"), "industry")

        response = admin.post(
            f"{API_V1_PREFIX}/p/s-alpha/audiences/upload",
            files={
                "file": (
                    "list.xlsx",
                    make_xlsx([["Name", "Phone"], ["A", "9876501071"]]),
                    XLSX_TYPE,
                )
            },
            data={"name": "Foreign segment", "segment_id": str(foreign)},
        )
        assert response.status_code == 404


# ---------------------------------------------------------------- inactive product


class TestInactiveProduct:
    def _inactive_world(self) -> World:
        world = World(make_product("s-paused"), phone="+919876501081")

        async def pending_audience_and_deactivate(session: AsyncSession) -> None:
            session.add(
                Audience(
                    name="Awaiting confirmation",
                    status=AudienceStatus.PENDING_CONFIRMATION,
                    product_id=world.product_id,
                    valid_rows=1,
                )
            )
            product = await session.get(Product, world.product_id)
            assert product is not None
            product.is_active = False

        run_db(pending_audience_and_deactivate)
        return world

    def test_records_stay_readable(self, admin: TestClient) -> None:
        world = self._inactive_world()
        base = f"{API_V1_PREFIX}/p/s-paused"

        assert admin.get(base).json()["is_active"] is False
        for resource in ("audiences", "campaigns", "leads", "conversations", "bookings"):
            assert admin.get(f"{base}/{resource}").status_code == 200, resource
        assert admin.get(f"{base}/campaigns/{world.campaign}").status_code == 200

    def test_new_customer_activity_is_refused(self, admin: TestClient) -> None:
        world = self._inactive_world()
        base = f"{API_V1_PREFIX}/p/s-paused"

        upload = admin.post(
            f"{base}/audiences/upload",
            files={
                "file": (
                    "list.xlsx",
                    make_xlsx([["Name", "Phone"], ["A", "9876501082"]]),
                    XLSX_TYPE,
                )
            },
            data={"name": "Refused"},
        )
        pending = next(
            a["id"]
            for a in admin.get(f"{base}/audiences").json()
            if a["status"] == "PENDING_CONFIRMATION"
        )
        confirm = admin.post(f"{base}/audiences/{pending}/confirm")
        create = admin.post(
            f"{base}/campaigns",
            json={
                "name": "Refused",
                "audience_id": str(world.audience),
                "template_id": str(world.template),
            },
        )
        send = admin.post(f"{base}/campaigns/{world.campaign}/send")

        for label, response in (
            ("upload", upload),
            ("confirm", confirm),
            ("create", create),
            ("send", send),
        ):
            assert response.status_code == 409, (label, response.text)
            assert "inactive" in response.text, label

    def test_configuration_and_stopping_work_stay_allowed(self, admin: TestClient) -> None:
        """Preparing a product before launch, and ending activity, are not new customer
        activity."""
        world = self._inactive_world()
        base = f"{API_V1_PREFIX}/p/s-paused"
        template = unassigned_template("prepared")

        knowledge = admin.post(
            f"{base}/knowledge-bases", json={"name": "Prepared", "description": None}
        )
        assign = admin.post(f"{base}/whatsapp-templates/{template}/assign")
        cancel = admin.post(f"{base}/campaigns/{world.campaign}/cancel")

        assert knowledge.status_code == 201, knowledge.text
        assert assign.status_code == 200, assign.text
        assert cancel.status_code == 200, cancel.text
