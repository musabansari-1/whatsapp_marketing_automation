"""HubSpot provider and CRM mapping tests (§14, §19, §20, [D11]).

Every request is served by a mock transport: no socket is opened and no HubSpot account
exists.

The identity handling is what deserves the attention. The platform's key is the phone
number ([D11]); HubSpot's own contact uniqueness is email-based, and a WhatsApp lead
often has no email — so "find, then create" has more ways to produce duplicates than it
looks.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.core.errors import NonRetryableProviderError, RetryableProviderError
from app.domain import crm as crm_domain
from app.integrations.crm.hubspot import HubSpotProvider
from app.integrations.crm.provider import ContactUpsert

PHONE = "+919876500500"
SYNCED_AT = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)


def make_provider(handler: Any) -> HubSpotProvider:
    return HubSpotProvider(
        access_token="secret-token",  # noqa: S106 - never leaves the mock
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def contact_body(contact_id: str = "1", **properties: Any) -> dict[str, Any]:
    return {
        "id": contact_id,
        "properties": {"phone": PHONE, "email": None, **properties},
    }


def routed(
    *,
    search_results: list[Any] | None = None,
    write: dict[str, Any] | None = None,
    write_status: int = 200,
) -> Any:
    """Answer the search endpoint and the create/update endpoints."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"results": search_results or []})
        return httpx.Response(write_status, json=write or contact_body())

    return handler


def an_upsert(**overrides: Any) -> ContactUpsert:
    defaults: dict[str, Any] = {
        "phone_e164": PHONE,
        "email": None,
        "properties": {"firstname": "Asha"},
    }
    return ContactUpsert(**{**defaults, **overrides})


class TestNameSplitting:
    def test_a_two_part_name_splits(self) -> None:
        assert crm_domain.split_name("Asha Rao") == ("Asha", "Rao")

    def test_a_single_name_stays_a_forename(self) -> None:
        """Guessing that "Asha" is a surname would be worse than leaving it unset."""
        assert crm_domain.split_name("Asha") == ("Asha", None)

    def test_a_three_part_name_keeps_the_last_token_as_surname(self) -> None:
        assert crm_domain.split_name("Asha Kumari Rao") == ("Asha Kumari", "Rao")

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_an_absent_name_yields_nothing(self, value: str | None) -> None:
        assert crm_domain.split_name(value) == (None, None)


class TestQualificationFormatting:
    def test_facts_render_as_readable_lines(self) -> None:
        """A salesperson reads this inside HubSpot, and JSON reads badly there."""
        text = crm_domain.format_qualification({"team_size": 40, "use_case": "support"})

        assert text is not None
        assert "team_size: 40" in text
        assert "use_case: support" in text

    def test_empty_extraction_yields_nothing(self) -> None:
        assert crm_domain.format_qualification({}) is None
        assert crm_domain.format_qualification(None) is None

    def test_empty_values_are_dropped(self) -> None:
        """The model emits null for what it did not learn; sending it would look like
        a collected fact with no value."""
        assert crm_domain.format_qualification({"team_size": None, "use_case": ""}) is None

    def test_a_long_extraction_is_truncated(self) -> None:
        """HubSpot's text properties are limited, and a silent rejection loses the
        whole write."""
        text = crm_domain.format_qualification({f"key_{n}": "x" * 50 for n in range(100)})

        assert text is not None
        assert len(text) <= crm_domain.MAX_QUALIFICATION_CHARS


class TestPropertyMapping:
    def test_the_phone_is_always_sent(self) -> None:
        """[D11]: it is the identity."""
        snapshot = crm_domain.LeadSnapshot(phone_e164=PHONE, status="RESPONDED")
        properties = crm_domain.build_properties(snapshot, synced_at=SYNCED_AT)

        assert properties[crm_domain.PROP_PHONE] == PHONE

    def test_opt_out_is_always_sent_including_false(self) -> None:
        """Consent is the field where a stale "yes" is dangerous, so it is never left
        to be inferred from absence."""
        snapshot = crm_domain.LeadSnapshot(phone_e164=PHONE, opted_out=False)
        properties = crm_domain.build_properties(snapshot, synced_at=SYNCED_AT)

        assert properties[crm_domain.PROP_OPTED_OUT] == "false"

    def test_unknown_fields_are_omitted_not_blanked(self) -> None:
        """HubSpot treats an empty string as a deliberate clear, so sending one would
        erase whatever a salesperson typed there by hand."""
        snapshot = crm_domain.LeadSnapshot(phone_e164=PHONE, company=None, city=None)
        properties = crm_domain.build_properties(snapshot, synced_at=SYNCED_AT)

        assert crm_domain.PROP_COMPANY not in properties
        assert crm_domain.PROP_CITY not in properties

    def test_every_section_14_field_is_mapped(self) -> None:
        """§14 names six things to synchronize. This is the one test that would catch
        one being dropped."""
        snapshot = crm_domain.LeadSnapshot(
            phone_e164=PHONE,
            full_name="Asha Rao",
            email="asha@example.com",
            company="Northwind",
            designation="Head of Support",
            city="Pune",
            status="DEMO_BOOKED",
            qualification_data={"team_size": 40},
            opted_out=False,
            source_audience_name="August batch",
            last_campaign_name="Launch",
            demo_start=SYNCED_AT,
            demo_status="CONFIRMED",
        )
        properties = crm_domain.build_properties(snapshot, synced_at=SYNCED_AT)

        # Contact information, lead state, qualification, source, campaign, booking,
        # opt-out.
        assert properties[crm_domain.PROP_EMAIL] == "asha@example.com"
        assert properties[crm_domain.PROP_LEAD_STATUS] == "DEMO_BOOKED"
        assert "team_size: 40" in properties[crm_domain.PROP_QUALIFICATION]
        assert properties[crm_domain.PROP_LEAD_SOURCE] == "August batch"
        assert properties[crm_domain.PROP_CAMPAIGN] == "Launch"
        assert properties[crm_domain.PROP_DEMO_STATUS] == "CONFIRMED"
        assert properties[crm_domain.PROP_OPTED_OUT] == "false"

    def test_an_inbound_lead_reports_its_own_source(self) -> None:
        """A lead that messaged first has no audience, and saying so beats a blank."""
        snapshot = crm_domain.LeadSnapshot(phone_e164=PHONE, source_audience_name=None)
        properties = crm_domain.build_properties(snapshot, synced_at=SYNCED_AT)

        assert properties[crm_domain.PROP_LEAD_SOURCE] == "WhatsApp inbound"

    def test_the_transcript_is_not_synchronized(self) -> None:
        """Deliberate: it is long, personal, and pushing it into a third-party system
        is a decision nobody made."""
        snapshot = crm_domain.LeadSnapshot(phone_e164=PHONE)
        properties = crm_domain.build_properties(snapshot, synced_at=SYNCED_AT)

        assert not any("transcript" in key or "message" in key for key in properties)


class TestIdentityResolution:
    @pytest.mark.asyncio
    async def test_the_phone_is_searched_first(self) -> None:
        """[D11] makes it the primary identity."""
        searched: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            if request.url.path.endswith("/search"):
                searched.append(json.loads(request.content))
                return httpx.Response(200, json={"results": []})
            return httpx.Response(200, json=contact_body())

        await make_provider(handler).find_contact(phone_e164=PHONE, email="asha@example.com")

        assert searched[0]["filterGroups"][0]["filters"][0]["propertyName"] == "phone"

    @pytest.mark.asyncio
    async def test_email_is_the_fallback(self) -> None:
        """HubSpot's own uniqueness is email-based, so a contact a salesperson created
        by hand will usually have an email and no WhatsApp number."""
        searched: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            if request.url.path.endswith("/search"):
                body = json.loads(request.content)
                name = body["filterGroups"][0]["filters"][0]["propertyName"]
                searched.append(name)
                if name == "email":
                    return httpx.Response(200, json={"results": [contact_body("42")]})
                return httpx.Response(200, json={"results": []})
            return httpx.Response(200, json=contact_body())

        found = await make_provider(handler).find_contact(
            phone_e164=PHONE, email="asha@example.com"
        )

        assert searched == ["phone", "email"]
        assert found is not None
        assert found.provider_contact_id == "42"

    @pytest.mark.asyncio
    async def test_no_match_returns_none(self) -> None:
        assert await make_provider(routed()).find_contact(phone_e164=PHONE) is None

    @pytest.mark.asyncio
    async def test_nothing_to_search_by_returns_none(self) -> None:
        assert await make_provider(routed()).find_contact() is None


class TestUpsert:
    @pytest.mark.asyncio
    async def test_a_new_contact_is_created(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/search"):
                return httpx.Response(200, json={"results": []})
            calls.append(request.method)
            return httpx.Response(200, json=contact_body("7"))

        contact = await make_provider(handler).upsert_contact(an_upsert())

        assert calls == ["POST"]
        assert contact.provider_contact_id == "7"

    @pytest.mark.asyncio
    async def test_an_existing_contact_is_updated_not_duplicated(self) -> None:
        """HubSpot would happily hold two contacts for one person, and a sales team
        working from duplicates is worse than no synchronization."""
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/search"):
                return httpx.Response(200, json={"results": [contact_body("7")]})
            calls.append(request.method)
            return httpx.Response(200, json=contact_body("7"))

        contact = await make_provider(handler).upsert_contact(an_upsert())

        assert calls == ["PATCH"]
        assert contact.provider_contact_id == "7"

    @pytest.mark.asyncio
    async def test_a_conflict_falls_back_to_updating(self) -> None:
        """Someone created the contact between the search and the write — a second
        worker, or a salesperson. §20 needs the retry to converge, not fail."""
        state = {"created": False}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/search"):
                # Absent on the first look, present after the conflict.
                results = [contact_body("9")] if state["created"] else []
                return httpx.Response(200, json={"results": results})
            if request.method == "POST":
                state["created"] = True
                return httpx.Response(409, json={"message": "Contact already exists"})
            return httpx.Response(200, json=contact_body("9"))

        contact = await make_provider(handler).upsert_contact(an_upsert())

        assert contact.provider_contact_id == "9"

    @pytest.mark.asyncio
    async def test_an_unresolvable_conflict_is_retryable(self) -> None:
        """A duplicate that cannot be found is a race worth another attempt, not a
        permanent failure."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/search"):
                return httpx.Response(200, json={"results": []})
            return httpx.Response(409, json={"message": "Contact already exists"})

        with pytest.raises(RetryableProviderError):
            await make_provider(handler).upsert_contact(an_upsert())

    @pytest.mark.asyncio
    async def test_the_phone_is_included_even_when_not_in_properties(self) -> None:
        sent: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            if request.url.path.endswith("/search"):
                return httpx.Response(200, json={"results": []})
            sent.update(json.loads(request.content))
            return httpx.Response(200, json=contact_body())

        await make_provider(handler).upsert_contact(
            ContactUpsert(phone_e164=PHONE, properties={"firstname": "Asha"})
        )

        assert sent["properties"]["phone"] == PHONE


class TestFailureClassification:
    @pytest.mark.asyncio
    async def test_rate_limiting_is_retryable(self) -> None:
        """HubSpot limits per second and per day; the request was never processed, so
        there is nothing to duplicate."""
        with pytest.raises(RetryableProviderError, match="Rate limited"):
            await make_provider(
                routed(write={"message": "slow down"}, write_status=429)
            ).upsert_contact(an_upsert())

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    async def test_client_errors_are_permanent(self, status: int) -> None:
        with pytest.raises(NonRetryableProviderError):
            await make_provider(
                routed(write={"message": "no"}, write_status=status)
            ).upsert_contact(an_upsert())

    @pytest.mark.asyncio
    async def test_server_errors_are_retryable(self) -> None:
        with pytest.raises(RetryableProviderError):
            await make_provider(routed(write={"message": "oops"}, write_status=503)).upsert_contact(
                an_upsert()
            )

    @pytest.mark.asyncio
    async def test_a_timeout_is_retryable(self) -> None:
        """The upsert searches before writing, so the retry updates rather than
        duplicates."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow")

        with pytest.raises(RetryableProviderError, match="timed out"):
            await make_provider(handler).upsert_contact(an_upsert())

    @pytest.mark.asyncio
    async def test_the_token_never_appears_in_an_error(self) -> None:
        """§19: an error message ends up in the log store."""
        with pytest.raises(NonRetryableProviderError) as caught:
            await make_provider(
                routed(write={"message": "denied"}, write_status=401)
            ).upsert_contact(an_upsert())

        assert "secret-token" not in str(caught.value)

    @pytest.mark.asyncio
    async def test_the_token_is_sent_as_a_bearer_header(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization", "")
            if request.url.path.endswith("/search"):
                return httpx.Response(200, json={"results": []})
            return httpx.Response(200, json=contact_body())

        await make_provider(handler).upsert_contact(an_upsert())
        assert seen["auth"] == "Bearer secret-token"
