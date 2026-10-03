"""HubSpot CRM provider (specification §14, §20, [D11]).

The real implementation behind `CRMProvider`. Nothing above `app.integrations` imports
this module; it is selected by configuration.

**HubSpot is an integration, not the database.** PostgreSQL stays the source of truth,
so this module only ever writes outward. It never reads a value back to change platform
state, which is what stops a stale or hand-edited CRM record overwriting what the
platform knows.

Identity is the [D11] problem in miniature. The platform's primary key is the
normalized phone number; HubSpot's own contact uniqueness is **email-based**, and a
WhatsApp lead often has no email at all. So lookup searches phone first, falls back to
email, and creates only when neither matches — otherwise two conversations with the same
person would become two contacts.

Access tokens are never logged (§19).
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.errors import NonRetryableProviderError, RetryableProviderError
from app.core.logging import get_logger
from app.integrations.crm.provider import ContactUpsert, CRMContact, CRMProvider
from app.integrations.http import shared_async_client

logger = get_logger(__name__)

DEFAULT_BASE_URL = "https://api.hubapi.com"

# Statuses that will not succeed on retry: a malformed request, a rejected token, a
# missing object. 409 is excluded deliberately — it means the contact already exists,
# which the upsert path handles rather than treating as failure.
_NON_RETRYABLE_STATUSES = frozenset({400, 401, 403, 404, 422})


class HubSpotProvider(CRMProvider):
    """Talks to the HubSpot CRM v3 API over HTTPS."""

    name = "hubspot"

    def __init__(
        self,
        *,
        access_token: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 20.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._access_token = access_token
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        # Injectable so tests supply a mock transport and no request reaches HubSpot.
        self._client = client

    # ---------------------------------------------------------------- interface

    async def find_contact(
        self, *, phone_e164: str | None = None, email: str | None = None
    ) -> CRMContact | None:
        """Find a contact by phone first, then email ([D11]).

        Phone first because it is the platform's identity. Email is the fallback
        because it is HubSpot's, and a contact created by a salesperson by hand will
        usually have an email and no WhatsApp number.
        """
        if phone_e164:
            found = await self._search("phone", phone_e164)
            if found is not None:
                return found

        if email:
            return await self._search("email", email)

        return None

    async def upsert_contact(self, upsert: ContactUpsert) -> CRMContact:
        """Create or update the contact for this phone number.

        Update-if-found rather than blind create: HubSpot would happily hold two
        contacts for one person, and a sales team working from duplicates is worse
        than no synchronization at all.
        """
        properties = dict(upsert.properties)
        properties.setdefault("phone", upsert.phone_e164)
        if upsert.email:
            properties.setdefault("email", upsert.email)

        existing = await self.find_contact(phone_e164=upsert.phone_e164, email=upsert.email)

        if existing is not None:
            body = await self._request(
                "PATCH",
                f"{self._base_url}/crm/v3/objects/contacts/{existing.provider_contact_id}",
                json={"properties": properties},
            )
            logger.info("crm_contact_updated", contact_id=existing.provider_contact_id)
            return _parse_contact(body)

        try:
            body = await self._request(
                "POST",
                f"{self._base_url}/crm/v3/objects/contacts",
                json={"properties": properties},
            )
        except _ConflictError:
            # Someone created the contact between the search and the write — a second
            # worker, or a salesperson. Re-reading and updating is what makes a
            # retried sync converge rather than fail (§20).
            found = await self.find_contact(phone_e164=upsert.phone_e164, email=upsert.email)
            if found is None:
                raise RetryableProviderError(
                    "HubSpot reported a duplicate contact that cannot be found",
                    provider=self.name,
                ) from None

            body = await self._request(
                "PATCH",
                f"{self._base_url}/crm/v3/objects/contacts/{found.provider_contact_id}",
                json={"properties": properties},
            )
            logger.info("crm_contact_updated_after_conflict", contact_id=found.provider_contact_id)
            return _parse_contact(body)

        logger.info("crm_contact_created", contact_id=body.get("id"))
        return _parse_contact(body)

    # ---------------------------------------------------------------- internals

    async def _search(self, property_name: str, value: str) -> CRMContact | None:
        """One exact-match search against a single property."""
        body = await self._request(
            "POST",
            f"{self._base_url}/crm/v3/objects/contacts/search",
            json={
                "filterGroups": [
                    {"filters": [{"propertyName": property_name, "operator": "EQ", "value": value}]}
                ],
                "properties": ["phone", "email", "firstname", "lastname"],
                # One is enough: a second match means duplicates already exist in
                # HubSpot, and picking the first is more useful than failing.
                "limit": 1,
            },
        )

        results = body.get("results") or []
        if not results:
            return None
        return _parse_contact(results[0])

    async def _request(
        self, method: str, url: str, *, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._access_token}"}

        try:
            if self._client is not None:
                response = await self._client.request(
                    method, url, json=json, headers=headers, timeout=self._timeout
                )
            else:
                response = await shared_async_client().request(
                    method, url, json=json, headers=headers, timeout=self._timeout
                )
        except httpx.TimeoutException as exc:
            # Says nothing about whether HubSpot processed it. Retryable — and because
            # the upsert searches before writing, a retry updates rather than
            # duplicates.
            raise RetryableProviderError(f"Request timed out: {exc}", provider=self.name) from exc
        except httpx.HTTPError as exc:
            raise RetryableProviderError(f"Network failure: {exc}", provider=self.name) from exc

        if response.status_code == 409:
            raise _ConflictError

        if response.is_success:
            if not response.content:
                return {}
            try:
                parsed: dict[str, Any] = response.json()
            except ValueError as exc:
                raise NonRetryableProviderError(
                    "HubSpot returned a success status with an unreadable body",
                    provider=self.name,
                ) from exc
            return parsed

        raise self._classify_error(response)

    def _classify_error(self, response: httpx.Response) -> Exception:
        """The §20 retryable / permanent split."""
        message = response.text[:300]
        category = ""
        try:
            body = response.json()
            message = body.get("message", message)
            category = body.get("category", "")
        except ValueError:
            pass

        if response.status_code == 429:
            # HubSpot rate limits per second and per day. Both are worth retrying:
            # the request was never processed, so there is nothing to duplicate.
            return RetryableProviderError(f"Rate limited: {message}", provider=self.name)
        if response.status_code in _NON_RETRYABLE_STATUSES:
            return NonRetryableProviderError(
                f"[{response.status_code}] {category} {message}".strip(), provider=self.name
            )
        if response.status_code >= 500:
            return RetryableProviderError(
                f"HubSpot server error {response.status_code}: {message}", provider=self.name
            )

        return RetryableProviderError(
            f"Unexpected status {response.status_code}: {message}", provider=self.name
        )


class _ConflictError(Exception):
    """HubSpot reported the contact as already existing (409)."""


def _parse_contact(body: dict[str, Any]) -> CRMContact:
    properties = body.get("properties") or {}
    return CRMContact(
        provider_contact_id=str(body.get("id", "")),
        phone_e164=properties.get("phone"),
        email=properties.get("email"),
        properties=dict(properties),
    )


__all__ = ["HubSpotProvider"]
