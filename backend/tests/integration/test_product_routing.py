"""Inbound product routing on a shared WABA (Phase 23, §28, [D40], [D45]).

One Meta App, one WhatsApp Business Account, several numbers under it — one per
product. So every product's messages arrive at the *same* webhook URL, verified by the
*same* app secret, inside an envelope whose entry `id` is the *same* WABA. The single
field that differs is `metadata.phone_number_id`: the number the customer wrote to.

That makes it the routing key, and makes these the tests that matter. The failure this
file exists to prevent is silent: a customer answered by the wrong product, in the wrong
voice, from a business they never contacted, with no field in the payload that would
later reveal the mistake.

Products are created dynamically here. Nothing depends on which product came first,
what it is called, or how many exist.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.domain.whatsapp_webhook import parse_webhook
from app.integrations.registry import build_whatsapp_provider
from app.integrations.whatsapp.meta import MetaWhatsAppProvider
from app.persistence.models import (
    AIRun,
    Conversation,
    ConversationMessage,
    Lead,
    Message,
    Product,
    WhatsAppSuppression,
)
from app.persistence.repositories import products as product_repo
from app.persistence.repositories import webhooks as webhook_repo
from tests.integration.helpers import run_db

pytestmark = pytest.mark.integration

ALPHA_NUMBER = "108000000000901"
BETA_NUMBER = "108000000000902"
CUSTOMER = "+919876500777"


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    async def clear(session: AsyncSession) -> None:
        await session.execute(delete(AIRun))
        await session.execute(delete(ConversationMessage))
        await session.execute(delete(Message))
        await session.execute(delete(Conversation))
        await session.execute(delete(WhatsAppSuppression))
        await session.execute(delete(Lead))
        await session.execute(delete(Product).where(Product.slug.like("r-%")))

    run_db(clear)
    yield
    run_db(clear)


def make_product(slug: str, number: str | None, *, is_active: bool = True) -> uuid.UUID:
    async def create(session: AsyncSession) -> uuid.UUID:
        product = Product(
            slug=slug,
            display_name=slug.upper(),
            is_active=is_active,
            whatsapp_phone_number_id=number,
        )
        session.add(product)
        await session.flush()
        return product.id

    return run_db(create)


def route(number: str | None) -> uuid.UUID:
    async def resolve(session: AsyncSession) -> uuid.UUID:
        product = await product_repo.product_for_receiving_number(session, number)
        return product.id

    return run_db(resolve)


class TestTheRoutingKey:
    def test_two_products_on_one_waba_are_told_apart_by_their_numbers(self) -> None:
        """The whole point of the phase.

        Both deliveries are indistinguishable except for the receiving number — same
        WABA, same secret, same endpoint — and they must still land in different
        products.
        """
        alpha = make_product("r-alpha", ALPHA_NUMBER)
        beta = make_product("r-beta", BETA_NUMBER)

        assert route(ALPHA_NUMBER) == alpha
        assert route(BETA_NUMBER) == beta

    def test_the_waba_is_not_the_key(self) -> None:
        """Recorded because the first design routed on the WABA, and with one shared
        WABA that resolves every product to the same one — the exact failure."""
        make_product("r-alpha", ALPHA_NUMBER)
        make_product("r-beta", BETA_NUMBER)

        payload_waba_ids = {"waba-1"}  # what both deliveries carry
        assert len(payload_waba_ids) == 1
        assert route(ALPHA_NUMBER) != route(BETA_NUMBER)


class TestFailingClosed:
    def test_an_unknown_number_is_refused(self) -> None:
        """A number nobody has configured. Refused rather than sent to whichever
        product exists, because a wrong answer here is invisible afterwards."""
        make_product("r-alpha", ALPHA_NUMBER)

        with pytest.raises(product_repo.UnknownReceivingNumberError):
            route("108000000009999")

    def test_a_missing_number_is_refused(self) -> None:
        make_product("r-alpha", ALPHA_NUMBER)

        with pytest.raises(product_repo.UnknownReceivingNumberError):
            route(None)

    def test_the_sole_product_is_not_a_default(self) -> None:
        """[D45] in its sharpest form.

        Elsewhere — internal authoring — a single active product may be assumed. Here it
        may not: authoring is a colleague who can be asked, while an inbound message
        arrives from a member of the public and a wrong guess starts a conversation with
        them under the wrong business.
        """
        make_product("r-alpha", ALPHA_NUMBER)

        with pytest.raises(product_repo.UnknownReceivingNumberError):
            route("108000000009999")

    def test_an_inactive_product_does_not_receive(self) -> None:
        """A deactivated product keeps its number recorded, for the audit trail, but
        must not take new conversations."""
        make_product("r-retired", ALPHA_NUMBER, is_active=False)

        with pytest.raises(product_repo.UnknownReceivingNumberError):
            route(ALPHA_NUMBER)

    def test_the_error_names_the_number(self) -> None:
        """The only way an operator finds the missing configuration."""
        with pytest.raises(product_repo.UnknownReceivingNumberError) as caught:
            route("108000000009999")

        assert "108000000009999" in str(caught.value)


class TestOneNumberOneProduct:
    def test_two_active_products_cannot_share_a_number(self) -> None:
        """Enforced in the schema, not in the application, because a duplicate here
        would make routing ambiguous in a way no later check could repair."""
        make_product("r-alpha", ALPHA_NUMBER)

        with pytest.raises(Exception, match="uq_products_active_whatsapp_number|unique"):
            make_product("r-beta", ALPHA_NUMBER)

    def test_an_inactive_product_may_keep_a_number_a_live_one_now_uses(self) -> None:
        """The index is partial for this reason: reassigning a number after a product
        is retired must not require erasing what it used to be."""
        make_product("r-retired", ALPHA_NUMBER, is_active=False)
        alpha = make_product("r-alpha", ALPHA_NUMBER)

        assert route(ALPHA_NUMBER) == alpha

    def test_several_products_may_have_no_number(self) -> None:
        """A product that does not use WhatsApp at all. NULLs are distinct, so the
        unique index does not collide — and the partial predicate says so explicitly."""
        make_product("r-alpha", None)
        make_product("r-beta", None)


class TestTheSameCustomerAcrossProducts:
    def test_one_person_writing_to_two_products_becomes_two_leads(self) -> None:
        """[D41]: identity is (product, phone), so the same human is a separate lead in
        each product — a SustainX enquiry must not appear in Academicae's pipeline."""
        alpha = make_product("r-alpha", ALPHA_NUMBER)
        beta = make_product("r-beta", BETA_NUMBER)

        async def both(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
            first = await webhook_repo.get_or_create_lead(
                session, product_id=alpha, phone_e164=CUSTOMER
            )
            second = await webhook_repo.get_or_create_lead(
                session, product_id=beta, phone_e164=CUSTOMER
            )
            return first.id, second.id

        first, second = run_db(both)
        assert first != second

    def test_writing_to_the_same_product_twice_reuses_the_lead(self) -> None:
        alpha = make_product("r-alpha", ALPHA_NUMBER)

        async def twice(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
            first = await webhook_repo.get_or_create_lead(
                session, product_id=alpha, phone_e164=CUSTOMER
            )
            second = await webhook_repo.get_or_create_lead(
                session, product_id=alpha, phone_e164=CUSTOMER
            )
            return first.id, second.id

        first, second = run_db(twice)
        assert first == second

    def test_each_lead_keeps_its_own_conversation(self) -> None:
        """Two products, one person, two threads — because the reply to each has to go
        out from the number that product owns."""
        alpha = make_product("r-alpha", ALPHA_NUMBER)
        beta = make_product("r-beta", BETA_NUMBER)

        async def converse(session: AsyncSession) -> list[uuid.UUID | None]:
            products: list[uuid.UUID | None] = []
            for product_id in (alpha, beta):
                lead = await webhook_repo.get_or_create_lead(
                    session, product_id=product_id, phone_e164=CUSTOMER
                )
                conversation = await webhook_repo.get_or_create_conversation(session, lead)
                products.append(conversation.product_id)
            return products

        assert run_db(converse) == [alpha, beta]

    def test_a_global_stop_still_suppresses_both(self) -> None:
        """[D42]: consent is about a person, not a product.

        Deliberately *not* symmetrical with lead identity. Someone who says STOP has
        told the organisation to leave them alone, and answering "that was only our
        other brand" is the reading that gets a business reported.
        """
        alpha = make_product("r-alpha", ALPHA_NUMBER)
        beta = make_product("r-beta", BETA_NUMBER)

        async def opt_out_of_one(session: AsyncSession) -> None:
            for product_id in (alpha, beta):
                await webhook_repo.get_or_create_lead(
                    session, product_id=product_id, phone_e164=CUSTOMER
                )
            lead = (
                await session.execute(
                    select(Lead).where(Lead.product_id == alpha, Lead.phone_e164 == CUSTOMER)
                )
            ).scalar_one()
            await webhook_repo.suppress(session, lead=lead, source_message_id="wamid.stop")

        run_db(opt_out_of_one)

        async def suppressed(session: AsyncSession) -> int:
            rows = (
                await session.execute(
                    select(WhatsAppSuppression).where(WhatsAppSuppression.phone_e164 == CUSTOMER)
                )
            ).scalars()
            return len(list(rows))

        # One suppression row for the person, not one per product.
        assert run_db(suppressed) == 1

        async def opted_out_flags(session: AsyncSession) -> list[bool]:
            rows = (
                await session.execute(
                    select(Lead.opted_out)
                    .where(Lead.phone_e164 == CUSTOMER)
                    .order_by(Lead.opted_out)
                )
            ).scalars()
            return list(rows)

        assert run_db(opted_out_flags) == [True, True], "consent is global ([D42])"


class TestOutboundNumberSelection:
    """The provider a product sends through.

    Read against the Meta provider rather than the fake, because the fake ignores
    configuration — and it is precisely the configuration that must differ.
    """

    def _settings(self) -> Settings:
        return Settings(
            whatsapp_provider="meta",
            whatsapp_access_token="shared-token",  # noqa: S106 - test fixture
            whatsapp_phone_number_id="108000000000000",
            whatsapp_business_account_id="waba-1",
            whatsapp_app_secret="s",  # noqa: S106 - test fixture
            whatsapp_verify_token="v",  # noqa: S106 - test fixture
            openrouter_model="test/model",
        )

    def _provider(self, product: Product | None) -> MetaWhatsAppProvider:
        provider = build_whatsapp_provider(self._settings(), product=product)
        assert isinstance(provider, MetaWhatsAppProvider)
        return provider

    def test_each_product_sends_from_its_own_number(self) -> None:
        alpha = Product(slug="r-alpha", display_name="A", whatsapp_phone_number_id=ALPHA_NUMBER)
        beta = Product(slug="r-beta", display_name="B", whatsapp_phone_number_id=BETA_NUMBER)

        assert self._provider(alpha)._phone_number_id == ALPHA_NUMBER
        assert self._provider(beta)._phone_number_id == BETA_NUMBER

    def test_two_providers_do_not_share_state(self) -> None:
        """Nothing caches a provider by settings alone. If anything ever does, this is
        the test that catches the second product being handed the first one's number."""
        alpha = Product(slug="r-alpha", display_name="A", whatsapp_phone_number_id=ALPHA_NUMBER)
        beta = Product(slug="r-beta", display_name="B", whatsapp_phone_number_id=BETA_NUMBER)

        first = self._provider(alpha)
        second = self._provider(beta)

        assert first is not second
        assert first._phone_number_id == ALPHA_NUMBER, "building beta must not mutate alpha"

    def test_the_credential_is_shared_because_the_app_is(self) -> None:
        """One Meta App, so one access token. Only the number is per-product."""
        alpha = Product(slug="r-alpha", display_name="A", whatsapp_phone_number_id=ALPHA_NUMBER)
        beta = Product(slug="r-beta", display_name="B", whatsapp_phone_number_id=BETA_NUMBER)

        assert self._provider(alpha)._access_token == self._provider(beta)._access_token

    def test_a_product_without_a_number_falls_back_to_configuration(self) -> None:
        """Not a routing decision — this is the pre-product path, still used wherever
        no product is in hand, and it reaches the platform's own number only."""
        assert self._provider(None)._phone_number_id == "108000000000000"


class TestTheParserFeedsTheRouter:
    def test_a_delivery_routes_end_to_end_by_its_metadata(self) -> None:
        """Parser and router joined up: what Meta sends resolves to a product without
        anything in between supplying the identity."""
        alpha = make_product("r-alpha", ALPHA_NUMBER)

        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "waba-1",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": ALPHA_NUMBER},
                                "contacts": [{"wa_id": "919876500777", "profile": {"name": "R"}}],
                                "messages": [
                                    {
                                        "id": "wamid.route",
                                        "from": "919876500777",
                                        "timestamp": "1788000000",
                                        "type": "text",
                                        "text": {"body": "Hello"},
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }

        event = parse_webhook(payload).inbound[0]
        assert route(event.phone_number_id) == alpha
