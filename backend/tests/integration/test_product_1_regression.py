"""[D54] — the existing product's behaviour must not change.

This is the standing guard for the whole multi-product migration. It drives the
marketing path end to end through the ordinary API, exactly as it worked before §28
existed, and records the outcome as a fingerprint: states, counts, statuses and stored
content.

**It deliberately touches nothing product-aware.** No segment is selected and no new table
is read. That is the point — while the existing product is the only configured one,
every one of these calls must keep behaving as it always did. Since Phase 26 the product
appears in the address, because every dashboard route lives under one; it is found by
lookup, never by slug. A later phase that changes a status or drops a recipient turns
this red.

Written in Phase 17, when it necessarily passes, so that it is already in place and
already meaningful before Phase 18 begins moving data. A guard written after the change
it is meant to guard proves nothing.

**Since Phase 27 it runs on imported configuration.** The environment no longer supplies
the product's spreadsheet format or AI policy; the product's own rows do. So before every
test the product's configuration is replaced by what `import-legacy-config` copies from an
environment that set nothing — which is what the product ran with before — and the guard
proves that configuration behaves as the environment did.

If a later phase makes this fail, the phase is wrong until proven otherwise — not the
test.
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

from app.configuration.legacy import (
    FORMER_DEFAULT_SYSTEM_PROMPT,
    LegacyEnvironment,
    import_legacy_configuration,
    read_legacy_configuration,
)
from app.core.config import Settings
from app.domain.ai_config import ResolvedAIConfig
from app.domain.enums import (
    AudienceStatus,
    CampaignStatus,
    ContactValidationStatus,
    LeadStatus,
    RecipientStatus,
    TemplateCategory,
)
from app.persistence.models import (
    AIConfiguration,
    Audience,
    AudienceContact,
    Campaign,
    CampaignRecipient,
    Lead,
    Product,
    WhatsAppSuppression,
    WhatsAppTemplate,
)
from app.persistence.repositories import ai_configurations as ai_config_repo
from app.persistence.repositories.readiness import platform_inputs_from_settings
from tests.integration.conftest import login
from tests.integration.helpers import ensure_product_id, run_db, scoped_prefix

pytestmark = pytest.mark.integration

XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# One valid row, one that must be rejected for a missing name ([D25]), one duplicate.
GOOD = "9876511000"
SECOND = "9876511001"
NAMELESS = "9876511002"


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
def _clean(seeded_users: dict[str, str], seeded_product: uuid.UUID) -> Iterator[None]:
    """`seeded_product` is a precondition, not decoration.

    [D54] is "while this is the only product, nothing changes", and the compatibility
    rule that makes it true reads the *count* of active products: exactly one, and a
    silent request is unambiguous in fact. A real database always has the product the
    migration seeds; the suite builds its schema with `create_all`, which seeds nothing,
    so the fixture supplies it.

    Named here rather than relied on from whichever test file happened to run first —
    that made this file pass or fail depending on collection order.
    """

    async def clear(session: AsyncSession) -> None:
        await session.execute(delete(CampaignRecipient))
        await session.execute(delete(Campaign))
        await session.execute(delete(WhatsAppSuppression))
        await session.execute(delete(AudienceContact))
        await session.execute(delete(Lead))
        await session.execute(delete(Audience))
        await session.execute(delete(WhatsAppTemplate))

    run_db(clear)
    run_db(import_the_legacy_configuration)
    yield
    run_db(clear)
    from app.api.deps import reset_queue_provider

    reset_queue_provider()


async def import_the_legacy_configuration(session: AsyncSession) -> None:
    """Replace the product's configuration with what the operator's import produces.

    Removes whatever the fixtures configured, then runs the same import an operator runs,
    against an environment that sets none of the retired variables — the state in which the
    product ran on the platform's built-in defaults.
    """
    product_id = await ensure_product_id(session)
    await session.execute(
        delete(AIConfiguration).where(
            AIConfiguration.product_id == product_id, AIConfiguration.segment_id.is_(None)
        )
    )
    product = await session.get(Product, product_id)
    assert product is not None
    product.audience_column_schema = None
    await session.flush()

    await import_legacy_configuration(
        session,
        product,
        read_legacy_configuration(LegacyEnvironment()),
        overwrite=False,
        platform=platform_inputs_from_settings(Settings()),
    )


@pytest.fixture
def admin(client: TestClient, seeded_users: dict[str, str]) -> TestClient:
    login(client)
    return client


def seed_template() -> str:
    async def insert(session: AsyncSession) -> str:
        template = WhatsAppTemplate(
            name="regression_promo",
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


class TestTheMarketingPathIsUnchanged:
    """Upload, validate, confirm, promote, campaign, snapshot — one pass, no product."""

    def test_the_whole_path_produces_the_expected_fingerprint(self, admin: TestClient) -> None:
        template_id = seed_template()

        rows: list[Sequence[object]] = [
            ["Name", "Phone"],
            ["Asha", GOOD],
            ["Ben", SECOND],
            # No name: [D25] makes it required, so this row must be INVALID.
            ["", NAMELESS],
            # A repeat of a number already seen in this file.
            ["Asha again", GOOD],
        ]

        # ---- upload -------------------------------------------------------
        upload = admin.post(
            f"{scoped_prefix()}/audiences/upload",
            files={"file": ("regression.xlsx", make_xlsx(rows), XLSX_TYPE)},
            data={"name": "Regression audience"},
        )
        assert upload.status_code == 201, upload.text
        body = upload.json()["audience"]
        audience_id = str(body["id"])

        assert body["status"] == AudienceStatus.PENDING_CONFIRMATION.value
        assert body["total_rows"] == 4
        assert body["valid_rows"] == 2
        assert body["invalid_rows"] == 1
        assert body["duplicate_rows"] == 1

        # Upload creates no leads ([D31]).
        assert self._lead_count() == 0

        # ---- confirm ------------------------------------------------------
        confirm = admin.post(f"{scoped_prefix()}/audiences/{audience_id}/confirm")
        assert confirm.status_code == 200, confirm.text
        assert self._lead_count() == 2

        # Phone normalization is unchanged, and the canonical fields survive promotion.
        leads = self._leads()
        assert set(leads) == {f"+91{GOOD}", f"+91{SECOND}"}
        assert leads[f"+91{GOOD}"] == LeadStatus.NEW

        # ---- campaign -----------------------------------------------------
        created = admin.post(
            f"{scoped_prefix()}/campaigns",
            json={
                "name": "Regression broadcast",
                "audience_id": audience_id,
                "template_id": template_id,
            },
        )
        assert created.status_code == 201, created.text
        detail = created.json()
        campaign_id = str(detail["campaign"]["id"])

        # READY is automatic once there is a sendable recipient ([D34]) and means
        # prepared, not sending.
        assert detail["campaign"]["status"] == CampaignStatus.READY.value
        assert detail["pending_recipients"] == 2

        # ---- snapshot -----------------------------------------------------
        recipients = self._recipients(uuid.UUID(campaign_id))
        assert recipients == {
            f"+91{GOOD}": RecipientStatus.PENDING,
            f"+91{SECOND}": RecipientStatus.PENDING,
        }

        # Nothing was sent. §7 requires an explicit human action.
        assert detail["campaign"]["sent_count"] == 0

    def test_suppression_still_skips_at_snapshot(self, admin: TestClient) -> None:
        """[D16] — global, phone-level, checked before the campaign is prepared.

        Kept in the regression guard because [D42] preserves this behaviour across the
        whole multi-product migration, and Phase 20 changes the code around it.
        """
        template_id = seed_template()

        async def suppress(session: AsyncSession) -> None:
            session.add(WhatsAppSuppression(phone_e164=f"+91{SECOND}"))

        run_db(suppress)

        rows: list[Sequence[object]] = [["Name", "Phone"], ["Asha", GOOD], ["Ben", SECOND]]
        upload = admin.post(
            f"{scoped_prefix()}/audiences/upload",
            files={"file": ("s.xlsx", make_xlsx(rows), XLSX_TYPE)},
            data={"name": "Suppressed"},
        )
        audience_id = str(upload.json()["audience"]["id"])
        admin.post(f"{scoped_prefix()}/audiences/{audience_id}/confirm")

        created = admin.post(
            f"{scoped_prefix()}/campaigns",
            json={
                "name": "Suppression regression",
                "audience_id": audience_id,
                "template_id": template_id,
            },
        )
        campaign_id = uuid.UUID(created.json()["campaign"]["id"])

        assert self._recipients(campaign_id) == {
            f"+91{GOOD}": RecipientStatus.PENDING,
            f"+91{SECOND}": RecipientStatus.SKIPPED,
        }

    def test_permissions_are_unchanged(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """[D33] — a salesperson may view campaigns and may not create one.

        The multi-product work adds a product dimension to authorization ([D46] keeps
        every user able to see every product); the role rules themselves must not move.
        """
        login(client, seeded_users["sales"])

        created = client.post(
            f"{scoped_prefix()}/campaigns",
            json={
                "name": "Should be refused",
                "audience_id": str(uuid.uuid4()),
                "template_id": str(uuid.uuid4()),
            },
        )
        assert created.status_code == 403

        assert client.get(f"{scoped_prefix()}/campaigns").status_code == 200

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _lead_count() -> int:
        async def read(session: AsyncSession) -> int:
            result = await session.execute(select(Lead))
            return len(list(result.scalars().all()))

        return run_db(read)

    @staticmethod
    def _leads() -> dict[str, LeadStatus]:
        async def read(session: AsyncSession) -> dict[str, LeadStatus]:
            result = await session.execute(select(Lead.phone_e164, Lead.status))
            return dict(result.all())  # type: ignore[arg-type]

        return run_db(read)

    @staticmethod
    def _recipients(campaign_id: uuid.UUID) -> dict[str, RecipientStatus]:
        async def read(session: AsyncSession) -> dict[str, RecipientStatus]:
            result = await session.execute(
                select(CampaignRecipient.phone_e164, CampaignRecipient.status).where(
                    CampaignRecipient.campaign_id == campaign_id
                )
            )
            return dict(result.all())  # type: ignore[arg-type]

        return run_db(read)


class TestTheConversationPolicyIsUnchanged:
    """Phase 27 deleted the rule that let the sole product use the environment's prompt and
    criteria. The imported configuration must resolve to exactly what that rule supplied."""

    def test_the_imported_policy_resolves_as_the_environment_did(self) -> None:
        settings = Settings()

        async def resolve(session: AsyncSession) -> ResolvedAIConfig:
            return await ai_config_repo.resolve_for(
                session,
                product_id=await ensure_product_id(session),
                segment_id=None,
                defaults=ai_config_repo.platform_defaults_from_settings(settings),
            )

        # Field by field, what the pre-Phase-27 resolution produced for the sole product
        # with no configuration row: the environment's prompt and criteria (their defaults,
        # since none was set) and the platform's operational values.
        assert run_db(resolve) == ResolvedAIConfig(
            system_prompt=FORMER_DEFAULT_SYSTEM_PROMPT,
            qualification_required_fields=(),
            model=settings.openrouter_model,
            rag_top_k=settings.rag_top_k,
            rag_min_similarity=settings.rag_min_similarity,
            rag_max_context_chars=settings.rag_max_context_chars,
            history_turns=settings.conversation_history_turns,
            max_tool_rounds=settings.ai_max_tool_rounds,
            fallback_message=settings.msg_rag_fallback,
        )


class TestTheAuditTrailIsUnchanged:
    def test_rejected_rows_are_still_stored_with_their_reason(self, admin: TestClient) -> None:
        """§6 requires the preview to show what was rejected and why."""
        rows: list[Sequence[object]] = [
            ["Name", "Phone"],
            ["Asha", GOOD],
            ["", NAMELESS],
            ["Asha again", GOOD],
        ]
        upload = admin.post(
            f"{scoped_prefix()}/audiences/upload",
            files={"file": ("audit.xlsx", make_xlsx(rows), XLSX_TYPE)},
            data={"name": "Audit"},
        )
        audience_id = uuid.UUID(upload.json()["audience"]["id"])

        async def read(session: AsyncSession) -> dict[str, int]:
            result = await session.execute(
                select(AudienceContact.validation_status).where(
                    AudienceContact.audience_id == audience_id
                )
            )
            counts: dict[str, int] = {}
            for (status,) in result.all():
                counts[status.value] = counts.get(status.value, 0) + 1
            return counts

        assert run_db(read) == {
            ContactValidationStatus.VALID.value: 1,
            ContactValidationStatus.INVALID.value: 1,
            ContactValidationStatus.DUPLICATE.value: 1,
        }
