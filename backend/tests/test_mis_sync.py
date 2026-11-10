"""MIS Tracker sync: mapping, client and configuration.

Every request is served by a mock transport; no MIS is contacted.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.core.config import CRMProviderName, Settings
from app.core.errors import NonRetryableProviderError, RetryableProviderError
from app.domain import mis as mis_domain
from app.domain.crm import LeadSnapshot
from app.domain.enums import LeadStatus
from app.integrations.crm.mis import MISClient

URL = "https://mis.example/api/wa-event"


def a_snapshot(**overrides: Any) -> LeadSnapshot:
    defaults: dict[str, Any] = {
        "phone_e164": "+919876500700",
        "full_name": "Dr. Asha Rao",
        "company": "Rao Ayurveda",
        "status": "INTERESTED",
        "qualification_data": {"speciality": "Ayurveda", "city": ""},
        "source_audience_name": "Delhi doctors",
        "last_campaign_name": "Doctor onboarding",
        "segment_name": "Healthcare Practitioners",
    }
    return LeadSnapshot(**{**defaults, **overrides})


class TestEngagement:
    @pytest.mark.parametrize("status", ["NEW", "CONTACTED", "FAILED"])
    def test_campaign_side_statuses_are_not_engaged(self, status: str) -> None:
        assert mis_domain.is_engaged(status) is False

    @pytest.mark.parametrize(
        "status", ["RESPONDED", "INTERESTED", "DEMO_BOOKED", "NOT_INTERESTED", "OPTED_OUT"]
    )
    def test_a_reply_or_its_outcome_is_engaged(self, status: str) -> None:
        assert mis_domain.is_engaged(LeadStatus(status)) is True


class TestEvent:
    def test_carries_identity_status_and_context(self) -> None:
        data = mis_domain.build_event(
            a_snapshot(),
            lead_id="L1",
            product_slug="life-herba",
            dashboard_url="https://wa.example/",
        )
        assert data["ref"] == "L1"
        assert data["phone"] == "+919876500700"
        assert data["status"] == "INTERESTED"
        assert data["opted_out"] is False
        assert data["person_name"] == "Dr. Asha Rao"
        assert data["campaign"] == "Doctor onboarding"
        assert data["segment"] == "Healthcare Practitioners"
        assert data["summary"] == "speciality: Ayurveda"
        assert data["link"] == "https://wa.example/p/life-herba/leads/L1"

    def test_omits_what_the_platform_does_not_know(self) -> None:
        data = mis_domain.build_event(
            a_snapshot(
                company=None, qualification_data={}, last_campaign_name=None, segment_name=None
            ),
            lead_id="L1",
            product_slug="life-herba",
        )
        assert "organisation" not in data
        assert "summary" not in data
        assert "campaign" not in data
        assert "segment" not in data
        assert "link" not in data

    def test_demo_time_is_iso(self) -> None:
        start = datetime(2026, 11, 10, 5, 30, tzinfo=UTC)
        data = mis_domain.build_event(
            a_snapshot(demo_start=start), lead_id="L1", product_slug="life-herba"
        )
        assert data["demo_at"] == start.isoformat()


def client_answering(status: int, body: Any, seen: list[httpx.Request] | None = None) -> MISClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=body)

    return MISClient(
        events_url=URL, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


class TestClient:
    @pytest.mark.asyncio
    async def test_posts_workspace_data_and_secret(self) -> None:
        seen: list[httpx.Request] = []
        client = client_answering(200, {"ok": True, "lead_id": "m1", "created": True}, seen)

        answer = await client.post_event(workspace="lifeherba", secret="s3", data={"ref": "L1"})

        assert answer["lead_id"] == "m1"
        assert seen[0].headers["x-mis-secret"] == "s3"
        assert json.loads(seen[0].content) == {"workspace": "lifeherba", "data": {"ref": "L1"}}

    @pytest.mark.asyncio
    async def test_a_rejected_secret_is_permanent(self) -> None:
        client = client_answering(401, {"error": "Unauthorised"})
        with pytest.raises(NonRetryableProviderError, match="Unauthorised"):
            await client.post_event(workspace="lifeherba", secret="bad", data={})

    @pytest.mark.parametrize("status", [429, 500, 503])
    @pytest.mark.asyncio
    async def test_overload_and_outages_are_retryable(self, status: int) -> None:
        client = client_answering(status, {"error": "busy"})
        with pytest.raises(RetryableProviderError):
            await client.post_event(workspace="lifeherba", secret="s", data={})

    @pytest.mark.asyncio
    async def test_a_network_failure_is_retryable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down", request=request)

        client = MISClient(
            events_url=URL, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        with pytest.raises(RetryableProviderError):
            await client.post_event(workspace="lifeherba", secret="s", data={})


class TestConfiguration:
    def test_mis_needs_an_events_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CRM_PROVIDER", "mis")
        monkeypatch.delenv("MIS_EVENTS_URL", raising=False)
        with pytest.raises(ValueError, match="MIS_EVENTS_URL"):
            Settings()

    def test_maps_parse_from_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CRM_PROVIDER", "mis")
        monkeypatch.setenv("MIS_EVENTS_URL", URL)
        monkeypatch.setenv("MIS_WORKSPACES", '{"life-herba": "lifeherba"}')
        monkeypatch.setenv("MIS_SECRETS", '{"life-herba": "s3"}')
        settings = Settings()
        assert settings.crm_provider is CRMProviderName.MIS
        assert settings.mis_workspaces == {"life-herba": "lifeherba"}
        assert settings.mis_secrets["life-herba"].get_secret_value() == "s3"
