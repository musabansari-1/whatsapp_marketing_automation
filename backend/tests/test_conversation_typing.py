"""Typing indicator refresher unit tests.

The refresher keeps "typing…" alive across a long model turn and stops the
moment the turn ends, whatever the outcome. Timing-based by nature, so the
margins are wide: a loaded machine must not be able to flake them.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.core.config import Settings
from app.worker.handlers import conversation as conversation_module


def _stub_show(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    async def fake_show(settings: Settings, product_id: uuid.UUID, wamid: str | None) -> None:
        calls.append(wamid or "")

    monkeypatch.setattr(conversation_module, "_show_typing_indicator", fake_show)


async def test_refresher_refires_while_the_block_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    _stub_show(monkeypatch, calls)

    async with conversation_module._typing_indicator_alive(  # noqa: SLF001
        Settings(), uuid.uuid4(), "wamid-1", interval_seconds=0.05
    ):
        await asyncio.sleep(0.18)

    assert 2 <= len(calls) <= 5
    assert set(calls) == {"wamid-1"}


async def test_nothing_fires_on_a_fast_turn_or_after_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fast turn exits before any refresh is due, and exiting stops the task:
    nothing may fire after the block, or the indicator would outlive the turn."""
    calls: list[str] = []
    _stub_show(monkeypatch, calls)

    async with conversation_module._typing_indicator_alive(  # noqa: SLF001
        Settings(), uuid.uuid4(), "wamid-1", interval_seconds=0.05
    ):
        pass

    await asyncio.sleep(0.12)
    assert calls == []
