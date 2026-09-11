"""Campaign endpoint tests (specification §7, §16, [D6], [D16]).

Templates are inserted directly as fixtures, never through an API. [D6] puts
approval with Meta and the synchronization in Phase 6, so nothing in this phase may
create or approve a template — the fixtures stand in for what that sync will provide.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Iterator, Sequence

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from openpyxl import Workbook
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.main import API_V1_PREFIX
from app.domain.enums import CampaignStatus, RecipientStatus, TemplateCategory
from app.persistence.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    Lead,
    WhatsAppSuppression,
    WhatsAppTemplate,
)
from tests.integration.conftest import login
from tests.integration.helpers import ensure_product_id, run_db

pytestmark = pytest.mark.integration

XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


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
        await session.execute(delete(Lead))
        await session.execute(delete(Audience))
        await session.execute(delete(WhatsAppTemplate))

    run_db(clear)
    yield
    run_db(clear)
    from app.api.deps import reset_queue_provider

    reset_queue_provider()


def seed_template(
    *,
    name: str = "q3_promo",
    provider_status: str = "APPROVED",
    category: TemplateCategory = TemplateCategory.MARKETING,
    assigned: bool = True,
) -> str:
    """Insert a template as the Meta sync will, then assign it as an admin would.

    Direct insertion is deliberate: [D6] forbids this application creating or
    approving templates, so there is no endpoint to do it through.

    Assigned to the test's product by default, because since [D57] an unassigned
    template is usable by no campaign. `assigned=False` leaves it exactly as the sync
    stores a newly discovered one.
    """

    async def insert(session: AsyncSession) -> str:
        template = WhatsAppTemplate(
            name=name,
            language="en",
            product_id=await ensure_product_id(session) if assigned else None,
            provider_template_id=f"tpl-{name}-{provider_status}-{category.value}",
            provider_status=provider_status,
            category=category,
            variable_count=1,
            body_preview="Hello {{1}}, see our Q3 offer.",
        )
        session.add(template)
        await session.flush()
        return str(template.id)

    return run_db(insert)


def seed_audience(client: TestClient, phones: Sequence[str]) -> str:
    """Upload and confirm an audience, so it has leads to snapshot."""
    rows: list[Sequence[object]] = [["Name", "Phone"]]
    rows.extend([f"Person {index}", phone] for index, phone in enumerate(phones))

    response = client.post(
        f"{API_V1_PREFIX}/audiences/upload",
        files={"file": ("list.xlsx", make_xlsx(rows), XLSX_TYPE)},
        data={"name": "Campaign audience"},
    )
    audience_id = str(response.json()["audience"]["id"])
    client.post(f"{API_V1_PREFIX}/audiences/{audience_id}/confirm")
    return audience_id


def create_campaign(client: TestClient, audience_id: str, template_id: str) -> Response:
    response: Response = client.post(
        f"{API_V1_PREFIX}/campaigns",
        json={"name": "Q3 broadcast", "audience_id": audience_id, "template_id": template_id},
    )
    return response


PHONES = ["9876543210", "9876543211", "9876543212"]


class TestPermissions:
    """[D33]: mass broadcast sending is ADMIN-only."""

    def test_a_salesperson_cannot_create_a_campaign(self, client: TestClient) -> None:
        login(client, "sales@example.com")
        audience_id = seed_audience(client, PHONES)
        assert create_campaign(client, audience_id, seed_template()).status_code == 403

    def test_a_salesperson_cannot_send_a_broadcast(self, client: TestClient) -> None:
        """The decision's whole point: a salesperson must not be able to trigger a
        mass broadcast, accidentally or otherwise."""
        login(client)
        audience_id = seed_audience(client, PHONES)
        campaign_id = create_campaign(client, audience_id, seed_template()).json()["campaign"]["id"]
        client.post(f"{API_V1_PREFIX}/auth/logout")

        login(client, "sales@example.com")
        assert client.post(f"{API_V1_PREFIX}/campaigns/{campaign_id}/send").status_code == 403

    def test_a_salesperson_cannot_cancel(self, client: TestClient) -> None:
        login(client)
        audience_id = seed_audience(client, PHONES)
        campaign_id = create_campaign(client, audience_id, seed_template()).json()["campaign"]["id"]
        client.post(f"{API_V1_PREFIX}/auth/logout")

        login(client, "sales@example.com")
        assert client.post(f"{API_V1_PREFIX}/campaigns/{campaign_id}/cancel").status_code == 403

    def test_a_salesperson_may_still_view_campaigns(self, client: TestClient) -> None:
        """[D33] restricts sending, not visibility."""
        login(client)
        audience_id = seed_audience(client, PHONES)
        campaign_id = create_campaign(client, audience_id, seed_template()).json()["campaign"]["id"]
        client.post(f"{API_V1_PREFIX}/auth/logout")

        login(client, "sales@example.com")
        assert client.get(f"{API_V1_PREFIX}/campaigns").status_code == 200
        assert client.get(f"{API_V1_PREFIX}/campaigns/{campaign_id}").status_code == 200
        assert client.get(f"{API_V1_PREFIX}/campaigns/{campaign_id}/recipients").status_code == 200


class TestCancellation:
    """[D29]."""

    def _ready(self, client: TestClient) -> str:
        audience_id = seed_audience(client, PHONES)
        return str(create_campaign(client, audience_id, seed_template()).json()["campaign"]["id"])

    def test_a_running_campaign_can_be_cancelled(self, client: TestClient) -> None:
        login(client)
        campaign_id = self._ready(client)
        client.post(f"{API_V1_PREFIX}/campaigns/{campaign_id}/send")

        response = client.post(f"{API_V1_PREFIX}/campaigns/{campaign_id}/cancel")
        assert response.status_code == 200
        assert response.json()["status"] == "CANCELLED"

    def test_cancelling_stops_unsent_recipients(self, client: TestClient) -> None:
        """The dashboard must reflect the cancellation immediately rather than waiting
        for every queued task to drain."""
        login(client)
        campaign_id = self._ready(client)
        client.post(f"{API_V1_PREFIX}/campaigns/{campaign_id}/send")
        client.post(f"{API_V1_PREFIX}/campaigns/{campaign_id}/cancel")

        page = client.get(f"{API_V1_PREFIX}/campaigns/{campaign_id}/recipients").json()
        assert {r["status"] for r in page["items"]} == {"SKIPPED"}

    def test_a_ready_campaign_can_be_cancelled_before_sending(self, client: TestClient) -> None:
        login(client)
        campaign_id = self._ready(client)
        assert client.post(f"{API_V1_PREFIX}/campaigns/{campaign_id}/cancel").status_code == 200

    def test_cancellation_is_idempotent(self, client: TestClient) -> None:
        """A double click or a retried request must not fail."""
        login(client)
        campaign_id = self._ready(client)
        client.post(f"{API_V1_PREFIX}/campaigns/{campaign_id}/cancel")

        second = client.post(f"{API_V1_PREFIX}/campaigns/{campaign_id}/cancel")
        assert second.status_code == 200
        assert second.json()["status"] == "CANCELLED"

    def test_a_cancelled_campaign_cannot_be_sent(self, client: TestClient) -> None:
        login(client)
        campaign_id = self._ready(client)
        client.post(f"{API_V1_PREFIX}/campaigns/{campaign_id}/cancel")

        assert client.post(f"{API_V1_PREFIX}/campaigns/{campaign_id}/send").status_code == 409

    def test_an_unknown_campaign_is_404(self, client: TestClient) -> None:
        login(client)
        assert client.post(f"{API_V1_PREFIX}/campaigns/{uuid.uuid4()}/cancel").status_code == 404


class TestAuthentication:
    def test_every_route_requires_a_session(self, client: TestClient) -> None:
        assert client.get(f"{API_V1_PREFIX}/campaigns").status_code == 401
        assert client.post(f"{API_V1_PREFIX}/campaigns", json={}).status_code == 401
        assert client.post(f"{API_V1_PREFIX}/campaigns/{uuid.uuid4()}/send").status_code == 401
        assert client.post(f"{API_V1_PREFIX}/campaigns/{uuid.uuid4()}/cancel").status_code == 401
        assert client.get(f"{API_V1_PREFIX}/whatsapp-templates").status_code == 401


class TestTemplateApproval:
    def test_only_approved_marketing_templates_are_listed(self, client: TestClient) -> None:
        """[D6]: the list is what Meta approved, nothing else."""
        login(client)
        seed_template(name="approved_marketing")
        seed_template(name="pending_one", provider_status="PENDING")
        seed_template(name="utility_one", category=TemplateCategory.UTILITY)

        names = {t["name"] for t in client.get(f"{API_V1_PREFIX}/whatsapp-templates").json()}
        assert names == {"approved_marketing"}

    def test_no_endpoint_creates_a_template(self, client: TestClient) -> None:
        """[D6] forbids the application inventing approval, so there is no way in."""
        login(client)
        assert client.post(f"{API_V1_PREFIX}/whatsapp-templates", json={}).status_code == 405

    def test_the_list_is_empty_before_the_meta_sync(self, client: TestClient) -> None:
        """Phase 6 populates it; until then a campaign cannot be created at all."""
        login(client)
        assert client.get(f"{API_V1_PREFIX}/whatsapp-templates").json() == []

    def test_an_unapproved_template_cannot_be_used(self, client: TestClient) -> None:
        login(client)
        audience_id = seed_audience(client, PHONES)
        template_id = seed_template(provider_status="PENDING")

        response = create_campaign(client, audience_id, template_id)
        assert response.status_code == 422
        assert "APPROVED" in response.json()["detail"]

    def test_a_utility_template_cannot_be_broadcast(self, client: TestClient) -> None:
        login(client)
        audience_id = seed_audience(client, PHONES)
        template_id = seed_template(category=TemplateCategory.UTILITY)

        response = create_campaign(client, audience_id, template_id)
        assert response.status_code == 422
        assert "MARKETING" in response.json()["detail"]


class TestCampaignCreation:
    def test_creating_freezes_the_recipient_snapshot(self, client: TestClient) -> None:
        """§7: later audience changes must not alter an existing campaign."""
        login(client)
        audience_id = seed_audience(client, PHONES)
        template_id = seed_template()

        response = create_campaign(client, audience_id, template_id)
        assert response.status_code == 201
        body = response.json()
        assert body["campaign"]["total_recipients"] == 3
        assert body["pending_recipients"] == 3
        assert body["campaign"]["status"] == "READY"

    def test_creation_sends_nothing(self, client: TestClient) -> None:
        """§7: sending is a separate, explicitly confirmed action."""
        from app.api.deps import get_queue_provider
        from app.core.config import Settings

        login(client)
        audience_id = seed_audience(client, PHONES)
        create_campaign(client, audience_id, seed_template())

        queue = get_queue_provider(Settings())
        assert queue.enqueued == []  # type: ignore[attr-defined]

    def test_the_template_is_returned_for_preview(self, client: TestClient) -> None:
        """§7 requires a preview before the broadcast is confirmed."""
        login(client)
        audience_id = seed_audience(client, PHONES)
        body = create_campaign(client, audience_id, seed_template()).json()

        assert body["template"]["name"] == "q3_promo"
        assert body["template"]["body_preview"]
        assert body["template"]["provider_status"] == "APPROVED"

    def test_an_unconfirmed_audience_is_refused(self, client: TestClient) -> None:
        """§6 puts admin confirmation before use, and its leads do not exist yet."""
        login(client)
        upload = client.post(
            f"{API_V1_PREFIX}/audiences/upload",
            files={
                "file": ("l.xlsx", make_xlsx([["Name", "Phone"], ["A", "9876543210"]]), XLSX_TYPE)
            },
            data={"name": "Unconfirmed"},
        )
        audience_id = str(upload.json()["audience"]["id"])

        response = create_campaign(client, audience_id, seed_template())
        assert response.status_code == 422
        assert "not been confirmed" in response.json()["detail"]

    def test_unknown_audience_and_template_are_404(self, client: TestClient) -> None:
        login(client)
        audience_id = seed_audience(client, PHONES)

        assert create_campaign(client, str(uuid.uuid4()), seed_template()).status_code == 404
        assert create_campaign(client, audience_id, str(uuid.uuid4())).status_code == 404

    def test_snapshot_is_independent_of_later_audience_changes(self, client: TestClient) -> None:
        """The point of §7's snapshot: a second audience adding leads must not grow
        an existing campaign."""
        login(client)
        audience_id = seed_audience(client, PHONES)
        campaign_id = create_campaign(client, audience_id, seed_template()).json()["campaign"]["id"]

        seed_audience(client, ["9876500001", "9876500002"])

        detail = client.get(f"{API_V1_PREFIX}/campaigns/{campaign_id}").json()
        assert detail["campaign"]["total_recipients"] == 3


class TestSuppressionAtSnapshot:
    def test_an_opted_out_number_is_skipped(self, client: TestClient) -> None:
        """[D16]: the global suppression list is consulted before sending."""
        login(client)

        async def suppress(session: AsyncSession) -> None:
            session.add(WhatsAppSuppression(phone_e164="+919876543211"))

        run_db(suppress)

        audience_id = seed_audience(client, PHONES)
        body = create_campaign(client, audience_id, seed_template()).json()

        assert body["campaign"]["total_recipients"] == 3
        assert body["campaign"]["skipped_count"] == 1
        assert body["pending_recipients"] == 2

    def test_skipped_is_not_failed(self, client: TestClient) -> None:
        """Respecting an opt-out is not a delivery failure."""
        login(client)

        async def suppress(session: AsyncSession) -> None:
            session.add(WhatsAppSuppression(phone_e164="+919876543210"))

        run_db(suppress)
        audience_id = seed_audience(client, PHONES)
        body = create_campaign(client, audience_id, seed_template()).json()

        assert body["campaign"]["failed_count"] == 0
        assert body["campaign"]["skipped_count"] == 1


class TestSendBroadcast:
    def _ready_campaign(self, client: TestClient) -> str:
        audience_id = seed_audience(client, PHONES)
        return str(create_campaign(client, audience_id, seed_template()).json()["campaign"]["id"])

    def test_send_enqueues_one_task_per_recipient(self, client: TestClient) -> None:
        from app.api.deps import get_queue_provider
        from app.core.config import Settings

        login(client)
        campaign_id = self._ready_campaign(client)

        response = client.post(f"{API_V1_PREFIX}/campaigns/{campaign_id}/send")
        assert response.status_code == 200
        assert response.json()["queued"] == 3
        assert response.json()["campaign"]["status"] == "PROCESSING"

        queue = get_queue_provider(Settings())
        assert len(queue.enqueued) == 3  # type: ignore[attr-defined]
        assert all(
            task.task_type.value == "send_campaign_message"
            for task in queue.enqueued  # type: ignore[attr-defined]
        )

    def test_nothing_is_sent_inside_the_request(self, client: TestClient) -> None:
        """§4: long-running work never happens in an HTTP request. No handler is
        registered for the task either, so the queue only records it."""
        from app.api.deps import get_queue_provider
        from app.core.config import Settings

        login(client)
        campaign_id = self._ready_campaign(client)
        client.post(f"{API_V1_PREFIX}/campaigns/{campaign_id}/send")

        queue = get_queue_provider(Settings())
        assert queue.executed == []  # type: ignore[attr-defined]

    def test_recipients_move_to_queued(self, client: TestClient) -> None:
        login(client)
        campaign_id = self._ready_campaign(client)
        client.post(f"{API_V1_PREFIX}/campaigns/{campaign_id}/send")

        page = client.get(f"{API_V1_PREFIX}/campaigns/{campaign_id}/recipients").json()
        assert {r["status"] for r in page["items"]} == {"QUEUED"}

    def test_resending_does_not_enqueue_anyone_twice(self, client: TestClient) -> None:
        """§20 idempotency: a re-triggered broadcast must not message anyone again."""
        from app.api.deps import get_queue_provider
        from app.core.config import Settings

        login(client)
        campaign_id = self._ready_campaign(client)
        client.post(f"{API_V1_PREFIX}/campaigns/{campaign_id}/send")

        second = client.post(f"{API_V1_PREFIX}/campaigns/{campaign_id}/send")
        # Already PROCESSING, so the second confirmation is refused outright.
        assert second.status_code == 409

        queue = get_queue_provider(Settings())
        assert len(queue.enqueued) == 3  # type: ignore[attr-defined]

    def test_task_ids_are_deterministic(self, client: TestClient) -> None:
        """A redelivered task must be recognizable as a repeat (§20)."""
        from app.api.deps import get_queue_provider
        from app.core.config import Settings

        login(client)
        campaign_id = self._ready_campaign(client)
        client.post(f"{API_V1_PREFIX}/campaigns/{campaign_id}/send")

        queue = get_queue_provider(Settings())
        ids = [task.task_id for task in queue.enqueued]  # type: ignore[attr-defined]
        assert all(task_id.startswith("send-") for task_id in ids)
        assert len(set(ids)) == len(ids)

    def test_a_draft_campaign_cannot_be_sent(self, client: TestClient) -> None:
        """A campaign with no recipients stays DRAFT and is not sendable."""
        login(client)
        audience_id = seed_audience(client, PHONES)

        async def wipe_leads(session: AsyncSession) -> None:
            await session.execute(delete(Lead))

        run_db(wipe_leads)

        campaign_id = create_campaign(client, audience_id, seed_template()).json()["campaign"]["id"]
        response = client.post(f"{API_V1_PREFIX}/campaigns/{campaign_id}/send")
        assert response.status_code == 409

    def test_unknown_campaign_is_404(self, client: TestClient) -> None:
        login(client)
        assert client.post(f"{API_V1_PREFIX}/campaigns/{uuid.uuid4()}/send").status_code == 404


class TestSuppressionRecheckBeforeSending:
    def test_opting_out_after_the_snapshot_still_prevents_a_send(self, client: TestClient) -> None:
        """[D16]: a customer can opt out between preparation and the broadcast. Without
        the re-check the campaign would message someone who already said stop."""
        from app.api.deps import get_queue_provider
        from app.core.config import Settings

        login(client)
        audience_id = seed_audience(client, PHONES)
        campaign_id = create_campaign(client, audience_id, seed_template()).json()["campaign"]["id"]

        async def suppress(session: AsyncSession) -> None:
            session.add(WhatsAppSuppression(phone_e164="+919876543212"))

        run_db(suppress)

        response = client.post(f"{API_V1_PREFIX}/campaigns/{campaign_id}/send")
        body = response.json()

        assert body["skipped_as_suppressed"] == 1
        assert body["queued"] == 2

        queue = get_queue_provider(Settings())
        assert len(queue.enqueued) == 2  # type: ignore[attr-defined]

    def test_the_suppressed_recipient_is_recorded_as_skipped(self, client: TestClient) -> None:
        login(client)
        audience_id = seed_audience(client, PHONES)
        campaign_id = create_campaign(client, audience_id, seed_template()).json()["campaign"]["id"]

        async def suppress(session: AsyncSession) -> None:
            session.add(WhatsAppSuppression(phone_e164="+919876543212"))

        run_db(suppress)
        client.post(f"{API_V1_PREFIX}/campaigns/{campaign_id}/send")

        page = client.get(
            f"{API_V1_PREFIX}/campaigns/{campaign_id}/recipients",
            params={"recipient_status": "SKIPPED"},
        ).json()
        assert page["total"] == 1
        assert page["items"][0]["phone_e164"] == "+919876543212"


class TestRetrieval:
    def test_campaigns_are_listed(self, client: TestClient) -> None:
        login(client)
        audience_id = seed_audience(client, PHONES)
        create_campaign(client, audience_id, seed_template())

        campaigns = client.get(f"{API_V1_PREFIX}/campaigns").json()
        assert len(campaigns) == 1
        assert campaigns[0]["name"] == "Q3 broadcast"

    def test_recipients_are_paginated_and_filterable(self, client: TestClient) -> None:
        login(client)
        audience_id = seed_audience(client, PHONES)
        campaign_id = create_campaign(client, audience_id, seed_template()).json()["campaign"]["id"]

        page = client.get(
            f"{API_V1_PREFIX}/campaigns/{campaign_id}/recipients", params={"limit": 2}
        ).json()
        assert len(page["items"]) == 2
        assert page["total"] == 3

        pending = client.get(
            f"{API_V1_PREFIX}/campaigns/{campaign_id}/recipients",
            params={"recipient_status": "PENDING"},
        ).json()
        assert pending["total"] == 3

    def test_unknown_campaign_detail_is_404(self, client: TestClient) -> None:
        login(client)
        assert client.get(f"{API_V1_PREFIX}/campaigns/{uuid.uuid4()}").status_code == 404


class TestSnapshotIntegrity:
    def test_recipients_carry_a_frozen_phone_number(self, client: TestClient) -> None:
        """§7: the snapshot copies the number so nothing downstream re-reads the
        audience."""
        login(client)
        audience_id = seed_audience(client, PHONES)
        create_campaign(client, audience_id, seed_template())

        async def phones(session: AsyncSession) -> list[str]:
            rows = (await session.execute(select(CampaignRecipient.phone_e164))).scalars().all()
            return sorted(rows)

        assert run_db(phones) == ["+919876543210", "+919876543211", "+919876543212"]

    def test_statuses_and_states_use_the_specified_names(self, client: TestClient) -> None:
        login(client)
        audience_id = seed_audience(client, PHONES)
        body = create_campaign(client, audience_id, seed_template()).json()

        assert body["campaign"]["status"] in {s.value for s in CampaignStatus}
        page = client.get(f"{API_V1_PREFIX}/campaigns/{body['campaign']['id']}/recipients").json()
        assert all(r["status"] in {s.value for s in RecipientStatus} for r in page["items"])
