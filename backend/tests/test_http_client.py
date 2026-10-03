"""Shared HTTP client behaviour.

One process-wide keep-alive client per event loop: repeated provider calls reuse
connections instead of paying a TLS handshake each time, and a loop change or a
closed client transparently yields a fresh one. Callers must never close it.
"""

from __future__ import annotations

import httpx

from app.integrations.http import shared_async_client


async def test_the_same_loop_reuses_one_client() -> None:
    first = shared_async_client()
    second = shared_async_client()

    assert first is second
    assert isinstance(first, httpx.AsyncClient)


async def test_a_closed_client_is_replaced() -> None:
    first = shared_async_client()
    await first.aclose()

    second = shared_async_client()

    assert second is not first
    assert not second.is_closed


async def test_providers_still_prefer_an_injected_client() -> None:
    """The mock-transport path is untouched: `client=...` wins over shared, so no
    test below this one can touch the network through the shared client."""
    from app.integrations.whatsapp.meta import MetaWhatsAppProvider
    from app.integrations.whatsapp.provider import OutboundTextMessage

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.X"}]})

    provider = MetaWhatsAppProvider(
        access_token="secret-token",  # noqa: S106 - never leaves the mock
        phone_number_id="phone-1",
        business_account_id="waba-1",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    result = await provider.send_text_message(
        OutboundTextMessage(to_e164="+919876543210", body="Hello")
    )

    assert result.provider_message_id == "wamid.X"
    assert len(seen) == 1
