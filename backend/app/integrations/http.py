"""Shared outbound HTTP client (latency).

Every provider used to open a fresh `httpx.AsyncClient` per request, paying a new
TLS handshake each time. This module holds one process-wide client with keep-alive,
so repeated calls to OpenRouter, Meta and Google reuse connections.

Loop-aware: a process may serve requests on different event loops over its
lifetime, and an httpx client is bound to its loop. The client is recreated when
the running loop changes or the client was closed. Callers must never close the
shared client — it carries no per-request state. Timeouts stay per-request at each
call site, exactly as before; the shared client itself sets none, so a site that
previously relied on the client default must pass its timeout explicitly.

Injected test clients (`client=...`) bypass this entirely: tests never touch the
network, shared or otherwise.
"""

from __future__ import annotations

import asyncio

import httpx

_client: httpx.AsyncClient | None = None
_loop: asyncio.AbstractEventLoop | None = None


def shared_async_client() -> httpx.AsyncClient:
    """Return the process-wide keep-alive client for the running loop."""
    global _client, _loop
    loop = asyncio.get_running_loop()
    if _client is None or _loop is not loop or _client.is_closed:
        _client = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20)
        )
        _loop = loop
    return _client


__all__ = ["shared_async_client"]
