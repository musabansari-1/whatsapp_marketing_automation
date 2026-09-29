"""Bot-activity scoring ([D67]).

Pure: given one conversation's inbound-message metadata, decide whether its pattern
looks automated. No message content is read — pace, regularity and marathon length
are enough, and reading content would trade customer privacy for nothing (a bot's
words look human; its timing does not).

The rule is deliberately explainable rather than clever. Each leg names a concrete
human limit it sits beyond:

- sustained rate: no negotiation needs 30 messages an hour, every hour;
- regularity: humans pause, reconsider, get interrupted — a 12-second median gap
  across dozens of messages is a loop, not a person;
- marathon length: a hundred-message history with a live correspondent is either a
  bot or a load test, and both deserve a human look.

A suspect verdict *flags*; it never gates. Flagging routes to a dashboard
notification the team reviews and dismisses or blocks on. A wrong guess therefore
costs a dismissed notification rather than a lost customer — which is also why an
owner load-testing one conversation can trip this, and should simply dismiss it.

Thresholds are configuration ([D67]), passed in rather than read, so the domain
stays testable without settings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class BotSignals:
    """One conversation's inbound shape over the audit window.

    `window_starts` are the ordered timestamps of inbound messages inside the
    trailing window (used for the median gap); `lifetime_messages` is the
    conversation's all-time inbound total. Only metadata — never content.
    """

    conversation_id: str
    window_messages: int
    window_starts: tuple[datetime, ...] = ()
    lifetime_messages: int = 0


@dataclass(frozen=True, slots=True)
class BotThresholds:
    """What counts as automated. All three are configuration."""

    msgs_per_hour: int = 30
    max_median_gap_seconds: int = 120
    min_lifetime_messages: int = 100


@dataclass(frozen=True, slots=True)
class BotVerdict:
    """The decision, with the reasons that fired so the flag explains itself."""

    suspect: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)


def median_gap_seconds(starts: tuple[datetime, ...]) -> float | None:
    """Median seconds between consecutive messages, or None when fewer than two."""
    ordered = sorted(starts)
    if len(ordered) < 2:
        return None
    gaps = [
        (later - earlier).total_seconds()
        for earlier, later in zip(ordered, ordered[1:], strict=False)
    ]
    middle = len(gaps) // 2
    if len(gaps) % 2:
        return float(gaps[middle])
    return (gaps[middle - 1] + gaps[middle]) / 2


def judge(signals: BotSignals, thresholds: BotThresholds) -> BotVerdict:
    """Suspect when the pace is inhuman AND the shape is machinelike.

    Pace alone is not enough (a burst of quick human replies exists); shape alone
    is not enough (two messages twelve seconds apart is ordinary). Sustained pace
    with machine regularity — or sustained pace across a marathon history — is the
    signature the staging incident showed: 44/hour at a 12-second median gap.
    """
    reasons: list[str] = []
    pace = signals.window_messages >= thresholds.msgs_per_hour
    if pace:
        reasons.append(
            f"{signals.window_messages} messages in the hour (limit {thresholds.msgs_per_hour})"
        )
    gap = median_gap_seconds(signals.window_starts)
    regular = gap is not None and gap <= thresholds.max_median_gap_seconds
    if regular and gap is not None:
        reasons.append(
            f"median gap {gap:.0f}s (machine-regular under {thresholds.max_median_gap_seconds}s)"
        )
    marathon = signals.lifetime_messages >= thresholds.min_lifetime_messages
    if marathon:
        reasons.append(
            f"{signals.lifetime_messages} messages lifetime "
            f"(marathon over {thresholds.min_lifetime_messages})"
        )
    return BotVerdict(suspect=pace and (regular or marathon), reasons=tuple(reasons))


__all__ = ["BotSignals", "BotThresholds", "BotVerdict", "judge", "median_gap_seconds"]
