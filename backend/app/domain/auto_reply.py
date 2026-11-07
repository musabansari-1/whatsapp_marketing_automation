"""Spotting a business's WhatsApp auto-reply, so the assistant does not argue with it.

Many clinics run WhatsApp Business with a greeting message that answers every incoming
message. When a campaign reaches one, the greeting comes back, the assistant answers
it, the greeting answers that, and the two talk to each other until someone notices:
Life Herba, 7 Nov 2026, "Try For Walk" sent the same greeting 40 times in 12 minutes
and got 40 replies.

A person rarely sends the same text three times in half an hour, and never sends eight
messages in ten minutes to a bot that is answering each one. Either pattern hands the
chat to the team instead of answering.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime, timedelta

#: The same text this many times (the current message included) inside the window.
REPEAT_THRESHOLD = 3
REPEAT_WINDOW = timedelta(minutes=30)

#: This many inbound messages (the current one included) inside the window, whatever
#: they say. Catches auto-replies that rotate between a few texts.
BURST_THRESHOLD = 8
BURST_WINDOW = timedelta(minutes=10)

#: How long the assistant stays quiet once a loop is spotted. The team is told, and can
#: resume it from the dashboard sooner.
PAUSE = timedelta(hours=24)

_SPACE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    return _SPACE.sub(" ", text).strip().casefold()


def looks_like_auto_reply(
    current: str,
    earlier: Iterable[tuple[datetime, str | None]],
    *,
    now: datetime,
) -> bool:
    """Whether the current inbound message is part of an automated reply loop.

    `earlier` is the conversation's previous inbound messages as (received at, body),
    not including the current one. Anything older than the longer window is ignored.
    """
    target = _normalise(current or "")
    repeats = 1
    burst = 1
    for received_at, body in earlier:
        age = now - received_at
        if age <= BURST_WINDOW:
            burst += 1
        if target and age <= REPEAT_WINDOW and _normalise(body or "") == target:
            repeats += 1
    return repeats >= REPEAT_THRESHOLD or burst >= BURST_THRESHOLD
