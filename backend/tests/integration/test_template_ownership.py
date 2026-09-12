"""Template ownership on a shared WABA ([D57], §28.8).

One Meta App and one WABA give one template pool, and Meta records no owner on any
template in it. So ownership is application data: a newly discovered template has no
product, and becomes usable only when someone assigns it to one.

The invariant under test is not "every template has a product". It is:

    every template a campaign uses is owned by that campaign's product

and the ways it could quietly fail are what these tests exercise — a NULL read as
"available to everyone", a re-sync that clears or moves an owner, a sync that assigns the
product that happened to trigger it, and a send of a campaign created before the rule.

Products are created dynamically. Nothing here depends on Academicae's identity.
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

from app.domain.campaign import TemplateAssignmentError
from app.domain.enums import RecipientStatus, TemplateCategory
from app.integrations.whatsapp import ApprovedTemplate
from app.persistence.models import (
    Audience,
    AudienceContact,
    Campaign,
    CampaignRecipient,
    Lead,
    Product,
    WhatsAppTemplate,
)
from app.persistence.repositories import templates as template_repo
from tests.integration.conftest import login
from tests.integration.helpers import run_db, scoped_prefix

pytestmark = pytest.mark.integration

XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PHONE = "9876547000"


@pytest.fixture(autouse=True)
def _clean(seeded_users: dict[str, str]) -> Iterator[None]:
    async def clear(session: AsyncSession) -> None:
        await session.execute(delete(CampaignRecipient))
        await session.execute(delete(Campaign))
        await session.execute(delete(AudienceContact))
        await session.execute(delete(Lead))
        await session.execute(delete(Audience))
        await session.execute(delete(WhatsAppTemplate))
        await session.execute(delete(Product).where(Product.slug.like("o-%")))

    run_db(clear)
    yield
    run_db(clear)
    from app.api.deps import reset_queue_provider

    reset_queue_provider()


@pytest.fixture
def admin(
    client: TestClient, seeded_users: dict[str, str], seeded_product: uuid.UUID
) -> TestClient:
    login(client)
    return client


# ---------------------------------------------------------------- helpers


def make_product(slug: str) -> uuid.UUID:
    async def create(session: AsyncSession) -> uuid.UUID:
        product = Product(slug=slug, display_name=slug.upper())
        session.add(product)
        await session.flush()
        return product.id

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


def upload_and_confirm(client: TestClient, product_id: uuid.UUID) -> str:
    response = client.post(
        f"{scoped_prefix(product_id)}/audiences/upload",
        files={"file": ("list.xlsx", make_xlsx([["Name", "Phone"], ["Person", PHONE]]), XLSX_TYPE)},
        data={"name": f"list-{uuid.uuid4().hex[:6]}"},
    )
    assert response.status_code == 201, response.text
    audience_id = str(response.json()["audience"]["id"])
    client.post(f"{scoped_prefix(product_id)}/audiences/{audience_id}/confirm")
    return audience_id


def seed_template(*, product_id: uuid.UUID | None, name: str = "welcome") -> str:
    """A template row as the sync stores it, optionally already assigned."""

    async def insert(session: AsyncSession) -> str:
        template = WhatsAppTemplate(
            name=name,
            language="en",
            product_id=product_id,
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


def create_campaign(client: TestClient, audience_id: str, template_id: str) -> tuple[int, str]:
    response = client.post(
        f"{scoped_prefix()}/campaigns",
        json={"name": "Broadcast", "audience_id": audience_id, "template_id": template_id},
    )
    return response.status_code, response.text


def synced(
    provider_id: str, *, status: str = "APPROVED", body: str = "Hello {{1}}."
) -> ApprovedTemplate:
    return ApprovedTemplate(
        name="welcome",
        language="en",
        category=TemplateCategory.MARKETING,
        provider_template_id=provider_id,
        variable_count=body.count("{{"),
        body_preview=body,
        provider_status=status,
    )


def run_sync(*templates: ApprovedTemplate) -> None:
    async def sync(session: AsyncSession) -> None:
        await template_repo.upsert_many(session, list(templates))

    run_db(sync)


def stored(provider_id: str) -> tuple[uuid.UUID | None, str, str | None]:
    async def read(session: AsyncSession) -> tuple[uuid.UUID | None, str, str | None]:
        row = (
            await session.execute(
                select(
                    WhatsAppTemplate.product_id,
                    WhatsAppTemplate.provider_status,
                    WhatsAppTemplate.body_preview,
                ).where(WhatsAppTemplate.provider_template_id == provider_id)
            )
        ).one()
        return row[0], row[1], row[2]

    return run_db(read)


def template_id_for(provider_id: str) -> uuid.UUID:
    async def read(session: AsyncSession) -> uuid.UUID:
        return (
            await session.execute(
                select(WhatsAppTemplate.id).where(
                    WhatsAppTemplate.provider_template_id == provider_id
                )
            )
        ).scalar_one()

    return run_db(read)


def assign(template_id: uuid.UUID, product_id: uuid.UUID) -> uuid.UUID | None:
    async def run(session: AsyncSession) -> uuid.UUID | None:
        template = await template_repo.assign_to_product(
            session, template_id=template_id, product_id=product_id
        )
        return template.product_id

    return run_db(run)


# ---------------------------------------------------------------- campaigns


class TestCampaignsRequireOwnership:
    def test_an_unassigned_template_cannot_be_used(
        self, admin: TestClient, seeded_product: uuid.UUID
    ) -> None:
        """The case that matters most. An unassigned template is not "available to all" —
        reading NULL that way would let any product broadcast any other's approved
        wording from its own number."""
        audience_id = upload_and_confirm(admin, seeded_product)
        template_id = seed_template(product_id=None)

        status, body = create_campaign(admin, audience_id, template_id)

        assert status == 422, body
        assert "not been assigned" in body

    def test_another_products_template_cannot_be_used(
        self, admin: TestClient, seeded_product: uuid.UUID
    ) -> None:
        other = make_product("o-other")
        audience_id = upload_and_confirm(admin, seeded_product)
        template_id = seed_template(product_id=other)

        status, body = create_campaign(admin, audience_id, template_id)

        assert status == 422, body
        assert "different product" in body

    def test_the_owning_products_campaign_can_use_it(
        self, admin: TestClient, seeded_product: uuid.UUID
    ) -> None:
        audience_id = upload_and_confirm(admin, seeded_product)
        template_id = seed_template(product_id=seeded_product)

        status, body = create_campaign(admin, audience_id, template_id)

        assert status == 201, body

    def test_a_campaign_whose_template_lost_its_owner_cannot_be_sent(
        self, admin: TestClient, seeded_product: uuid.UUID
    ) -> None:
        """A campaign created before [D57] may point at a template with no product.

        Refused at the human moment, before anything is queued — the last point where
        refusing costs nothing.
        """
        from app.api.deps import get_queue_provider
        from app.core.config import Settings

        audience_id = upload_and_confirm(admin, seeded_product)
        template_id = seed_template(product_id=seeded_product)
        created = admin.post(
            f"{scoped_prefix()}/campaigns",
            json={"name": "Broadcast", "audience_id": audience_id, "template_id": template_id},
        )
        assert created.status_code == 201, created.text
        campaign_id = str(created.json()["campaign"]["id"])

        async def as_before_the_rule(session: AsyncSession) -> None:
            template = await session.get(WhatsAppTemplate, uuid.UUID(template_id))
            assert template is not None
            template.product_id = None

        run_db(as_before_the_rule)

        response = admin.post(f"{scoped_prefix()}/campaigns/{campaign_id}/send")

        assert response.status_code == 409, response.text
        assert "not been assigned" in response.text
        assert get_queue_provider(Settings()).enqueued == []  # type: ignore[attr-defined]

        async def recipients(session: AsyncSession) -> list[RecipientStatus]:
            result = await session.execute(
                select(CampaignRecipient.status).where(
                    CampaignRecipient.campaign_id == uuid.UUID(campaign_id)
                )
            )
            return list(result.scalars().all())

        assert run_db(recipients) == [RecipientStatus.PENDING], "nothing was touched"

    def test_unassigned_templates_are_not_offered(
        self, admin: TestClient, seeded_product: uuid.UUID
    ) -> None:
        """Offering one would only produce a refusal after the admin chose it."""
        seed_template(product_id=seeded_product, name="assigned_one")
        seed_template(product_id=None, name="unassigned_one")

        names = {t["name"] for t in admin.get(f"{scoped_prefix()}/whatsapp-templates").json()}

        assert names == {"assigned_one"}


# ---------------------------------------------------------------- synchronization


class TestSynchronizationPreservesOwnership:
    def test_a_newly_discovered_template_is_stored_unassigned(self) -> None:
        """Nothing in Meta's record says whose it is, so the sync says nothing either."""
        run_sync(synced("tpl-new"))

        owner, status, _body = stored("tpl-new")
        assert owner is None
        assert status == "APPROVED"

    def test_a_resync_keeps_the_assignment_and_updates_what_meta_controls(
        self, seeded_product: uuid.UUID
    ) -> None:
        run_sync(synced("tpl-1"))
        assign(template_id_for("tpl-1"), seeded_product)

        # Meta pauses it and the wording changes.
        run_sync(synced("tpl-1", status="PAUSED", body="Hi {{1}}, updated."))

        owner, status, body = stored("tpl-1")
        assert owner == seeded_product, "a re-sync must never clear an assignment"
        assert status == "PAUSED", "provider-controlled fields still follow Meta"
        assert body == "Hi {{1}}, updated."

    def test_a_resync_never_moves_a_template_between_products(
        self, seeded_product: uuid.UUID
    ) -> None:
        """Status reconciliation is WABA-wide; ownership is not part of it."""
        other = make_product("o-other")
        run_sync(synced("tpl-a"), synced("tpl-b"), synced("tpl-c"))
        # Distinct names, since both would otherwise be `welcome` in `en`.

        async def rename(session: AsyncSession) -> None:
            for provider_id, name in (("tpl-a", "a"), ("tpl-b", "b"), ("tpl-c", "c")):
                template = (
                    await session.execute(
                        select(WhatsAppTemplate).where(
                            WhatsAppTemplate.provider_template_id == provider_id
                        )
                    )
                ).scalar_one()
                template.name = name

        run_db(rename)
        assign(template_id_for("tpl-a"), seeded_product)
        assign(template_id_for("tpl-b"), other)

        run_sync(synced("tpl-a", status="PAUSED"), synced("tpl-b"), synced("tpl-c"))

        assert stored("tpl-a")[0] == seeded_product
        assert stored("tpl-b")[0] == other
        assert stored("tpl-c")[0] is None, "an unrelated template is not claimed by anyone"

    def test_a_reissued_name_under_a_new_provider_id_does_not_break_sync(
        self, seeded_product: uuid.UUID
    ) -> None:
        """Meta can issue a deleted template's name again under a new ID.

        The global `(name, language)` rule this replaced turned that into a failed sync
        batch. Now the new artifact is simply another unassigned row, and the old one keeps
        its owner.
        """
        run_sync(synced("tpl-original"))
        assign(template_id_for("tpl-original"), seeded_product)

        run_sync(synced("tpl-reissued"))

        assert stored("tpl-original")[0] == seeded_product
        assert stored("tpl-reissued")[0] is None


# ---------------------------------------------------------------- assignment


class TestExplicitAssignment:
    def test_an_unassigned_template_is_assigned(self, seeded_product: uuid.UUID) -> None:
        template_id = uuid.UUID(seed_template(product_id=None))

        assert assign(template_id, seeded_product) == seeded_product

    def test_assigning_to_the_current_owner_changes_nothing(
        self, seeded_product: uuid.UUID
    ) -> None:
        template_id = uuid.UUID(seed_template(product_id=seeded_product))

        assert assign(template_id, seeded_product) == seeded_product

    def test_an_owned_template_cannot_be_moved(self, seeded_product: uuid.UUID) -> None:
        other = make_product("o-other")
        template_id = uuid.UUID(seed_template(product_id=seeded_product))

        with pytest.raises(TemplateAssignmentError, match="another product"):
            assign(template_id, other)

        assert stored_owner(template_id) == seeded_product

    def test_the_target_product_must_exist(self) -> None:
        template_id = uuid.UUID(seed_template(product_id=None))

        with pytest.raises(TemplateAssignmentError, match="Product not found"):
            assign(template_id, uuid.uuid4())

        assert stored_owner(template_id) is None

    def test_the_template_must_exist(self, seeded_product: uuid.UUID) -> None:
        with pytest.raises(TemplateAssignmentError, match="Template not found"):
            assign(uuid.uuid4(), seeded_product)

    def test_a_product_cannot_own_two_templates_with_one_name_and_language(
        self, seeded_product: uuid.UUID
    ) -> None:
        """ "Send `welcome` in `en`" must mean one template within a product."""
        seed_template(product_id=seeded_product, name="welcome")
        reissued = uuid.UUID(seed_template(product_id=None, name="welcome"))

        with pytest.raises(TemplateAssignmentError, match="already has a template"):
            assign(reissued, seeded_product)

    def test_two_products_may_each_own_a_template_of_the_same_name(
        self, seeded_product: uuid.UUID
    ) -> None:
        """The rule is per product. It makes no claim about what Meta allows in one WABA
        — only that a product's own namespace is unambiguous."""
        other = make_product("o-other")
        seed_template(product_id=seeded_product, name="welcome")
        second = uuid.UUID(seed_template(product_id=None, name="welcome"))

        assert assign(second, other) == other

    def test_the_database_is_the_backstop(self, seeded_product: uuid.UUID) -> None:
        """The partial unique index refuses a duplicate even if the check were bypassed."""
        seed_template(product_id=seeded_product, name="welcome")

        with pytest.raises(IntegrityError):
            seed_template(product_id=seeded_product, name="welcome")


def stored_owner(template_id: uuid.UUID) -> uuid.UUID | None:
    async def read(session: AsyncSession) -> uuid.UUID | None:
        template = await session.get(WhatsAppTemplate, template_id)
        assert template is not None
        return template.product_id

    return run_db(read)
