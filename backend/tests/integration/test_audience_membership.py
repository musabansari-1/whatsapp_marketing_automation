"""Audience membership and the recipient snapshot (Phase 16, [D55]).

A lead legitimately belongs to more than one audience: the same number appears in a
second uploaded file, and both audiences genuinely contain that person.

Before this phase the recipient snapshot selected leads by `Lead.source_audience_id`,
which records only where a lead was *first* seen and is never updated on re-import. A
campaign on a later audience therefore omitted every lead it shared with an earlier one
— silently, with no error and a recipient count that simply looked smaller than the
audience.

The first test here is that defect. It fails against the previous implementation.
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

from app.domain.enums import ContactValidationStatus, RecipientStatus, TemplateCategory
from app.persistence.models import (
    Audience,
    AudienceContact,
    Campaign,
    CampaignRecipient,
    Lead,
    WhatsAppSuppression,
    WhatsAppTemplate,
)
from tests.integration.conftest import login
from tests.integration.helpers import ensure_product_id, run_db, scoped_prefix

pytestmark = pytest.mark.integration

XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

SHARED = "9876500100"
ONLY_FIRST = "9876500101"
ONLY_SECOND = "9876500102"


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

    run_db(clear)
    yield
    run_db(clear)
    from app.api.deps import reset_queue_provider

    reset_queue_provider()


@pytest.fixture
def admin(client: TestClient, seeded_users: dict[str, str]) -> TestClient:
    """Signed in as ADMIN. Campaign creation is ADMIN-only ([D33])."""
    login(client)
    return client


def seed_template() -> str:
    async def insert(session: AsyncSession) -> str:
        template = WhatsAppTemplate(
            name="membership_promo",
            language="en",
            # Assigned, as an admin would after the sync discovered it ([D57]).
            product_id=await ensure_product_id(session),
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


def upload_and_confirm(client: TestClient, name: str, phones: Sequence[str]) -> str:
    """One audience, uploaded and confirmed, so its contacts become leads."""
    rows: list[Sequence[object]] = [["Name", "Phone"]]
    rows.extend([f"Person {phone}", phone] for phone in phones)

    response = client.post(
        f"{scoped_prefix()}/audiences/upload",
        files={"file": (f"{name}.xlsx", make_xlsx(rows), XLSX_TYPE)},
        data={"name": name},
    )
    assert response.status_code == 201, response.text
    audience_id = str(response.json()["audience"]["id"])

    confirmed = client.post(f"{scoped_prefix()}/audiences/{audience_id}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    return audience_id


def campaign_phones(client: TestClient, audience_id: str, template_id: str) -> set[str]:
    """Create a campaign and return the phone numbers actually snapshotted."""
    response = client.post(
        f"{scoped_prefix()}/campaigns",
        json={
            "name": f"Campaign for {audience_id[:8]}",
            "audience_id": audience_id,
            "template_id": template_id,
        },
    )
    assert response.status_code == 201, response.text
    campaign_id = uuid.UUID(response.json()["campaign"]["id"])

    async def read(session: AsyncSession) -> set[str]:
        result = await session.execute(
            select(CampaignRecipient.phone_e164).where(CampaignRecipient.campaign_id == campaign_id)
        )
        return set(result.scalars().all())

    return run_db(read)


class TestALeadInTwoAudiences:
    """The defect this phase fixes."""

    def test_a_shared_lead_appears_in_both_campaigns(self, admin: TestClient) -> None:
        """The regression. The shared number belongs to both audiences, so both
        campaigns must reach it.

        Against the previous implementation the second campaign omitted it: the lead's
        `source_audience_id` still pointed at the first audience.
        """
        template_id = seed_template()

        first = upload_and_confirm(admin, "first", [SHARED, ONLY_FIRST])
        second = upload_and_confirm(admin, "second", [SHARED, ONLY_SECOND])

        first_recipients = campaign_phones(admin, first, template_id)
        second_recipients = campaign_phones(admin, second, template_id)

        assert first_recipients == {f"+91{SHARED}", f"+91{ONLY_FIRST}"}
        assert second_recipients == {f"+91{SHARED}", f"+91{ONLY_SECOND}"}

    def test_the_shared_number_is_still_one_lead(self, admin: TestClient) -> None:
        """Membership in two audiences must not duplicate the lead ([D11])."""
        upload_and_confirm(admin, "first", [SHARED, ONLY_FIRST])
        upload_and_confirm(admin, "second", [SHARED, ONLY_SECOND])

        async def count(session: AsyncSession) -> int:
            result = await session.execute(select(Lead).where(Lead.phone_e164 == f"+91{SHARED}"))
            return len(list(result.scalars().all()))

        assert run_db(count) == 1

    def test_provenance_still_records_the_first_audience(self, admin: TestClient) -> None:
        """`source_audience_id` keeps its original meaning ([D55]).

        It is provenance — first seen in — and the CRM's `wa_lead_source` reads it. The
        fix must not repurpose or start updating it.
        """
        first = upload_and_confirm(admin, "first", [SHARED])
        upload_and_confirm(admin, "second", [SHARED])

        async def read(session: AsyncSession) -> str | None:
            lead = (
                await session.execute(select(Lead).where(Lead.phone_e164 == f"+91{SHARED}"))
            ).scalar_one()
            return str(lead.source_audience_id) if lead.source_audience_id else None

        assert run_db(read) == first


class TestMembershipLinking:
    def test_valid_contacts_are_linked_to_their_lead(self, admin: TestClient) -> None:
        audience_id = upload_and_confirm(admin, "linked", [SHARED, ONLY_FIRST])

        async def read(session: AsyncSession) -> list[tuple[str | None, uuid.UUID | None]]:
            result = await session.execute(
                select(AudienceContact.phone_e164, AudienceContact.lead_id).where(
                    AudienceContact.audience_id == uuid.UUID(audience_id)
                )
            )
            return list(result.all())  # type: ignore[arg-type]

        rows = run_db(read)
        assert len(rows) == 2
        assert all(lead_id is not None for _, lead_id in rows)

    def test_an_invalid_row_is_not_linked(self, admin: TestClient) -> None:
        """An INVALID row never became a lead, so it has no membership."""
        rows: list[Sequence[object]] = [["Name", "Phone"], ["No phone", ""]]
        response = admin.post(
            f"{scoped_prefix()}/audiences/upload",
            files={"file": ("bad.xlsx", make_xlsx(rows), XLSX_TYPE)},
            data={"name": "bad"},
        )
        audience_id = str(response.json()["audience"]["id"])
        admin.post(f"{scoped_prefix()}/audiences/{audience_id}/confirm")

        async def read(session: AsyncSession) -> list[tuple[ContactValidationStatus, object]]:
            result = await session.execute(
                select(AudienceContact.validation_status, AudienceContact.lead_id).where(
                    AudienceContact.audience_id == uuid.UUID(audience_id)
                )
            )
            return list(result.all())  # type: ignore[arg-type]

        rows_read = run_db(read)
        assert rows_read
        for status, lead_id in rows_read:
            assert status is ContactValidationStatus.INVALID
            assert lead_id is None

    def test_a_duplicate_row_is_not_linked(self, admin: TestClient) -> None:
        """A DUPLICATE repeats a number whose first occurrence is VALID and linked.

        Linking it too would add nothing and would risk a second recipient row for one
        lead if the snapshot's DISTINCT were ever removed.
        """
        rows: list[Sequence[object]] = [
            ["Name", "Phone"],
            ["First", SHARED],
            ["Again", SHARED],
        ]
        response = admin.post(
            f"{scoped_prefix()}/audiences/upload",
            files={"file": ("dupe.xlsx", make_xlsx(rows), XLSX_TYPE)},
            data={"name": "dupe"},
        )
        audience_id = str(response.json()["audience"]["id"])
        admin.post(f"{scoped_prefix()}/audiences/{audience_id}/confirm")

        async def read(session: AsyncSession) -> dict[str, int]:
            result = await session.execute(
                select(AudienceContact.validation_status, AudienceContact.lead_id).where(
                    AudienceContact.audience_id == uuid.UUID(audience_id)
                )
            )
            counts = {"linked": 0, "unlinked": 0}
            for _status, lead_id in result.all():
                counts["linked" if lead_id is not None else "unlinked"] += 1
            return counts

        assert run_db(read) == {"linked": 1, "unlinked": 1}

    def test_confirming_twice_links_the_same_rows(self, admin: TestClient) -> None:
        """Idempotent, as §20 requires of confirmation."""
        audience_id = upload_and_confirm(admin, "twice", [SHARED, ONLY_FIRST])
        admin.post(f"{scoped_prefix()}/audiences/{audience_id}/confirm")

        async def read(session: AsyncSession) -> int:
            result = await session.execute(
                select(AudienceContact.lead_id).where(
                    AudienceContact.audience_id == uuid.UUID(audience_id),
                    AudienceContact.lead_id.is_not(None),
                )
            )
            return len(list(result.scalars().all()))

        assert run_db(read) == 2


class TestSnapshotBehaviourIsOtherwiseUnchanged:
    def test_a_suppressed_number_is_still_skipped(self, admin: TestClient) -> None:
        """[D16] at snapshot time, unaffected by the membership change."""
        template_id = seed_template()

        async def suppress(session: AsyncSession) -> None:
            session.add(WhatsAppSuppression(phone_e164=f"+91{ONLY_FIRST}"))

        run_db(suppress)

        audience_id = upload_and_confirm(admin, "suppressed", [SHARED, ONLY_FIRST])
        response = admin.post(
            f"{scoped_prefix()}/campaigns",
            json={
                "name": "Suppression check",
                "audience_id": audience_id,
                "template_id": template_id,
            },
        )
        campaign_id = uuid.UUID(response.json()["campaign"]["id"])

        async def read(session: AsyncSession) -> dict[str, str]:
            result = await session.execute(
                select(CampaignRecipient.phone_e164, CampaignRecipient.status).where(
                    CampaignRecipient.campaign_id == campaign_id
                )
            )
            return {phone: status.value for phone, status in result.all()}

        statuses = run_db(read)
        assert statuses[f"+91{ONLY_FIRST}"] == RecipientStatus.SKIPPED.value
        assert statuses[f"+91{SHARED}"] == RecipientStatus.PENDING.value

    def test_an_unconfirmed_audience_snapshots_nothing(self, admin: TestClient) -> None:
        """Leads exist only after confirmation ([D31]), so membership does too."""
        template_id = seed_template()

        rows: list[Sequence[object]] = [["Name", "Phone"], ["Person", SHARED]]
        response = admin.post(
            f"{scoped_prefix()}/audiences/upload",
            files={"file": ("pending.xlsx", make_xlsx(rows), XLSX_TYPE)},
            data={"name": "pending"},
        )
        audience_id = str(response.json()["audience"]["id"])

        created = admin.post(
            f"{scoped_prefix()}/campaigns",
            json={
                "name": "Too early",
                "audience_id": audience_id,
                "template_id": template_id,
            },
        )
        if created.status_code == 201:
            campaign_id = uuid.UUID(created.json()["campaign"]["id"])

            async def read(session: AsyncSession) -> int:
                result = await session.execute(
                    select(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign_id)
                )
                return len(list(result.scalars().all()))

            assert run_db(read) == 0
        else:
            # Refusing an unconfirmed audience outright is equally correct.
            assert created.status_code in (400, 409, 422)
