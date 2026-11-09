"""MIS Tracker client: posts one lead event to `POST <MIS_EVENTS_URL>`.

The MIS authenticates each call with the target workspace's webhook secret (header
`x-mis-secret`) and upserts the record by our lead id, then by phone. Secrets are never
logged.

Same retry split as HubSpot (§20): network failures, 429 and 5xx are retryable; 4xx is
permanent and recorded on the lead.
"""

from __future__ import annotations

import contextlib
from typing import Any

import httpx

from app.core.errors import NonRetryableProviderError, RetryableProviderError
from app.integrations.http import shared_async_client

_NON_RETRYABLE_STATUSES = frozenset({400, 401, 403, 404, 422})


class MISClient:
    name = "mis"

    def __init__(
        self,
        *,
        events_url: str,
        timeout_seconds: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = events_url
        self._timeout = timeout_seconds
        # Injectable so tests supply a mock transport.
        self._client = client

    async def post_event(
        self, *, workspace: str, secret: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Send one lead; returns the MIS answer (`{ok, lead_id, created, ...}`)."""
        client = self._client or shared_async_client()
        try:
            response = await client.post(
                self._url,
                json={"workspace": workspace, "data": data},
                headers={"x-mis-secret": secret},
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            # The MIS upserts by ref, so a retry after an unknown outcome converges.
            raise RetryableProviderError(f"Request timed out: {exc}", provider=self.name) from exc
        except httpx.HTTPError as exc:
            raise RetryableProviderError(f"Network failure: {exc}", provider=self.name) from exc

        if response.is_success:
            try:
                body: dict[str, Any] = response.json()
            except ValueError as exc:
                raise NonRetryableProviderError(
                    "MIS returned a success status with an unreadable body", provider=self.name
                ) from exc
            return body

        message = response.text[:300]
        with contextlib.suppress(ValueError):
            message = str(response.json().get("error", message))
        if response.status_code in _NON_RETRYABLE_STATUSES:
            raise NonRetryableProviderError(
                f"[{response.status_code}] {message}", provider=self.name
            )
        raise RetryableProviderError(
            f"MIS status {response.status_code}: {message}", provider=self.name
        )


__all__ = ["MISClient"]
