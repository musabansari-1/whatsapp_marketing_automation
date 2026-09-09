"""Conversation product and segment snapshotting (Phase 22, §28.5, [D48]).

A conversation records the context it operates under, once, at creation. Product comes
from the lead — a conversation belongs to the same product as the person it is with.
Segment comes from the campaign that can be shown to have caused the reply, and stays
NULL when nothing proves one.

**Immutability is the point.** A lead may belong to several segments, and later
membership must not retarget a conversation already under way: a pharmacist who joins a
wellness-centre audience is still having a pharmacy conversation.

Products and segments are created dynamically. Nothing depends on Academicae's identity.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    CampaignStatus,
    ConversationStatus,
    RecipientStatus,
    TemplateCategory,
)
from app.persistence.models import (
    Audience,
    AudienceContact,
    Campaign,
    CampaignRecipient,
    Conversation,
    Lead,
    Product,
    Segment,
    WhatsAppSuppression,
    WhatsAppTemplate,
)
from app.persistence.repositories import webhooks as webhook_repo
from tests.integration.helpers import run_db

pytestmark = pytest.mark.integration

PHONE = "+919876555000"


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    async def clear(session: AsyncSession) -> None:
        await session.execute(delete(Conversation))
        await session.execute(delete(CampaignRecipient))
        await session.execute(delete(Campaign))
        await session.execute(delete(WhatsAppSuppression))
        await session.execute(delete(AudienceContact))
        await session.execute(delete(Lead))
        await session.execute(delete(Audience))
        await session.execute(delete(WhatsAppTemplate))
        await session.execute(delete(Segment))
        await session.execute(delete(Product).where(Product.slug.like("x-%")))

    run_db(clear)
    yield
    run_db(clear)


def make_product(slug: str) -> uuid.UUID:
    async def create(session: AsyncSession) -> uuid.UUID:
        product = Product(slug=slug, display_name=slug.upper())
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


def make_lead(product_id: uuid.UUID | None, phone: str = PHONE) -> uuid.UUID:
    async def create(session: AsyncSession) -> uuid.UUID:
        lead = Lead(product_id=product_id, phone_e164=phone, full_name="Someone")
        session.add(lead)
        await session.flush()
        return lead.id

    return run_db(create)


def messaged_by_campaign(
    lead_id: uuid.UUID,
    product_id: uuid.UUID,
    segment_id: uuid.UUID | None,
    *,
    name: str,
    status: RecipientStatus = RecipientStatus.DELIVERED,
) -> uuid.UUID:
    """A campaign that actually reached this lead — or didn't, per `status`."""

    async def create(session: AsyncSession) -> uuid.UUID:
        template = WhatsAppTemplate(
            name=f"tpl_{uuid.uuid4().hex[:6]}",
            language="en",
            provider_template_id=f"tpl-{uuid.uuid4()}",
            provider_status="APPROVED",
            category=TemplateCategory.MARKETING,
            variable_count=1,
        )
        audience = Audience(name=f"{name} audience", product_id=product_id, segment_id=segment_id)
        session.add_all([template, audience])
        await session.flush()

        campaign = Campaign(
            name=name,
            audience_id=audience.id,
            product_id=product_id,
            segment_id=segment_id,
            template_id=template.id,
            status=CampaignStatus.COMPLETED,
        )
        session.add(campaign)
        await session.flush()

        session.add(
            CampaignRecipient(
                campaign_id=campaign.id, lead_id=lead_id, phone_e164=PHONE, status=status
            )
        )
        await session.flush()
        return campaign.id

    return run_db(create)


def create_conversation(lead_id: uuid.UUID) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """Run the real creation path and return the snapshotted context."""

    async def run(session: AsyncSession) -> tuple[uuid.UUID | None, uuid.UUID | None]:
        lead = await session.get(Lead, lead_id)
        assert lead is not None
        conversation = await webhook_repo.get_or_create_conversation(session, lead)
        return conversation.product_id, conversation.segment_id

    return run_db(run)


class TestProductSnapshot:
    def test_a_conversation_takes_its_product_from_its_lead(self) -> None:
        product = make_product("x-alpha")
        lead = make_lead(product)

        assert create_conversation(lead) == (product, None)

    def test_a_conversation_never_crosses_products(self) -> None:
        """Lead in product A must not produce a conversation in product B."""
        alpha = make_product("x-alpha")
        make_product("x-beta")
        lead = make_lead(alpha)

        product_id, _segment = create_conversation(lead)
        assert product_id == alpha

    def test_a_lead_with_no_product_yet_gives_a_conversation_with_none(self) -> None:
        """The transitional inbound row [D41] leaves until Phase 23.

        NULL rather than a guess: [D45] forbids inventing a product, and the turn
        handler refuses to run product-dependent behaviour without one.
        """
        lead = make_lead(None)
        assert create_conversation(lead) == (None, None)


class TestSegmentAttribution:
    def test_one_campaign_that_reached_the_lead_supplies_the_segment(self) -> None:
        """Exactly one campaign messaged them, so exactly one can have prompted a
        reply."""
        product = make_product("x-herba")
        pharmacies = make_segment(product, "pharmacies")
        lead = make_lead(product)
        messaged_by_campaign(lead, product, pharmacies, name="Pharmacy Q1")

        assert create_conversation(lead) == (product, pharmacies)

    def test_a_product_wide_campaign_gives_no_segment(self) -> None:
        """Not ambiguity — the campaign genuinely targeted no segment."""
        product = make_product("x-alpha")
        lead = make_lead(product)
        messaged_by_campaign(lead, product, None, name="Everyone")

        assert create_conversation(lead) == (product, None)

    def test_two_campaigns_that_both_reached_the_lead_give_no_segment(self) -> None:
        """Ambiguous attribution stays NULL.

        No "most recent wins": nothing in §7 or §8 makes recency evidence, and running a
        conversation under the wrong segment's instructions is worse than running it
        under the product's shared defaults.
        """
        product = make_product("x-herba")
        pharmacies = make_segment(product, "pharmacies")
        wellness = make_segment(product, "wellness-centers")
        lead = make_lead(product)

        messaged_by_campaign(lead, product, pharmacies, name="Pharmacy Q1")
        messaged_by_campaign(lead, product, wellness, name="Wellness Q1")

        assert create_conversation(lead) == (product, None)

    def test_a_campaign_that_never_reached_the_lead_is_not_evidence(self) -> None:
        """A message that was never sent cannot have caused an answer.

        SKIPPED is the case that matters: a suppressed recipient was deliberately not
        messaged, so attributing a reply to that campaign would be nonsense.
        """
        product = make_product("x-herba")
        pharmacies = make_segment(product, "pharmacies")
        lead = make_lead(product)

        messaged_by_campaign(
            lead, product, pharmacies, name="Skipped", status=RecipientStatus.SKIPPED
        )

        assert create_conversation(lead) == (product, None)

    def test_one_delivered_campaign_among_unsent_ones_still_attributes(self) -> None:
        product = make_product("x-herba")
        pharmacies = make_segment(product, "pharmacies")
        wellness = make_segment(product, "wellness-centers")
        lead = make_lead(product)

        messaged_by_campaign(lead, product, pharmacies, name="Delivered")
        messaged_by_campaign(
            lead, product, wellness, name="Pending", status=RecipientStatus.PENDING
        )

        assert create_conversation(lead) == (product, pharmacies)

    def test_no_campaign_at_all_gives_no_segment(self) -> None:
        """An organic inbound lead nobody has messaged."""
        product = make_product("x-alpha")
        lead = make_lead(product)

        assert create_conversation(lead) == (product, None)


class TestSnapshotImmutability:
    def test_a_later_message_does_not_rederive_context(self) -> None:
        """The same conversation is returned untouched, whatever changed since."""
        product = make_product("x-herba")
        pharmacies = make_segment(product, "pharmacies")
        lead = make_lead(product)
        messaged_by_campaign(lead, product, pharmacies, name="Pharmacy Q1")

        first = create_conversation(lead)
        assert first == (product, pharmacies)

        # A second campaign arrives, which would now make attribution ambiguous.
        wellness = make_segment(product, "wellness-centers")
        messaged_by_campaign(lead, product, wellness, name="Wellness Q1")

        assert create_conversation(lead) == first, "an open conversation must not be re-derived"

    def test_new_audience_membership_does_not_retarget_an_open_conversation(self) -> None:
        """The [D48] case in full: one lead, two memberships, one operative context."""
        product = make_product("x-herba")
        pharmacies = make_segment(product, "pharmacies")
        lead = make_lead(product)
        messaged_by_campaign(lead, product, pharmacies, name="Pharmacy Q1")

        create_conversation(lead)

        wellness = make_segment(product, "wellness-centers")

        async def add_membership(session: AsyncSession) -> None:
            audience = Audience(name="Wellness list", product_id=product, segment_id=wellness)
            session.add(audience)
            await session.flush()
            session.add(
                AudienceContact(
                    audience_id=audience.id,
                    row_number=1,
                    phone_e164=PHONE,
                    lead_id=lead,
                    validation_status="VALID",
                    raw_row={},
                )
            )

        run_db(add_membership)

        async def read(session: AsyncSession) -> uuid.UUID | None:
            conversation = (
                await session.execute(select(Conversation).where(Conversation.lead_id == lead))
            ).scalar_one()
            return conversation.segment_id

        assert run_db(read) == pharmacies

    def test_a_closed_conversation_starts_a_new_one_with_current_context(self) -> None:
        """[D36] never reopens a closed conversation, so a later exchange legitimately
        establishes its own context."""
        product = make_product("x-herba")
        pharmacies = make_segment(product, "pharmacies")
        lead = make_lead(product)
        messaged_by_campaign(lead, product, pharmacies, name="Pharmacy Q1")

        create_conversation(lead)

        async def close(session: AsyncSession) -> None:
            conversation = (
                await session.execute(select(Conversation).where(Conversation.lead_id == lead))
            ).scalar_one()
            conversation.status = ConversationStatus.CLOSED

        run_db(close)

        # A fresh conversation, and it snapshots what is true now.
        assert create_conversation(lead) == (product, pharmacies)

        async def count(session: AsyncSession) -> int:
            result = await session.execute(select(Conversation).where(Conversation.lead_id == lead))
            return len(list(result.scalars().all()))

        assert run_db(count) == 2
