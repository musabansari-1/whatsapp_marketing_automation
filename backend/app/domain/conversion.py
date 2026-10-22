"""Product conversion strategy: how a conversation turns interest into an action.

Two modes, both configuration:

- ``calendar_booking`` — demo booking through Calendar (Academicae / SustainX shape).
- ``external_registration`` — nudge toward an external registration URL (Life Herba shape).

Nothing here branches on product slug. Readiness, tools and prompts read the mode from
the product row.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from zoneinfo import ZoneInfo


class ConversionMode(StrEnum):
    CALENDAR_BOOKING = "calendar_booking"
    EXTERNAL_REGISTRATION = "external_registration"


DEFAULT_CONVERSION_MODE = ConversionMode.CALENDAR_BOOKING


def parse_conversion_mode(value: str | None) -> ConversionMode:
    """Missing / blank inherits the platform default: calendar booking."""
    if value is None or not str(value).strip():
        return DEFAULT_CONVERSION_MODE
    return ConversionMode(str(value).strip())


def uses_calendar_booking(mode: ConversionMode | str | None) -> bool:
    return parse_conversion_mode(
        mode.value if isinstance(mode, ConversionMode) else mode
    ) is ConversionMode.CALENDAR_BOOKING


def with_today_context(system_prompt: str, *, now: datetime, zone: ZoneInfo) -> str:
    """Append today's date in the product's timezone.

    The model resolves relative days ("Friday", "day after tomorrow") into
    `from_date` values against this line. Without it, date-first booking is
    guesswork — staging offered undateable "tomorrow" slots on 2026-10-02.
    """
    today = now.astimezone(zone).strftime("%A %d %B %Y")
    return f"{system_prompt}\n\nToday is {today} ({zone.key})."


def registration_guidance(
    *,
    registration_url: str | None,
    cta_label: str | None,
    cta_instruction: str | None,
) -> str:
    """System-prompt appendix for external-registration products.

    Kept out of the base product prompt so the URL and segment CTA stay single-sourced
    in configuration. Empty when nothing is configured.
    """
    lines = [
        "## Registration conversion",
        "",
        "This product converts interest through external registration, not calendar booking.",
        "Do not offer, promise, or attempt to book a demo or meeting.",
        "Do not send the registration link merely because someone replied — answer first,",
        "understand needs, then nudge once genuine interest exists.",
        "If they directly ask how to join, register, download the app, or become a partner,",
        "send the registration destination immediately.",
        "Share the Registration URL exactly as given. The Segment registration instruction",
        "says what opening it does; describe that, not buttons or menus inside the app.",
    ]
    if registration_url:
        lines.extend(["", f"Registration URL: {registration_url}"])
    if cta_label:
        lines.extend(["", f"Segment CTA label: {cta_label}"])
    if cta_instruction:
        lines.extend(["", f"Segment registration instruction: {cta_instruction}"])
    return "\n".join(lines)


def with_conversion_guidance(
    system_prompt: str,
    *,
    mode: ConversionMode | str | None,
    registration_url: str | None = None,
    cta_label: str | None = None,
    cta_instruction: str | None = None,
) -> str:
    """Append registration guidance when the product uses external registration."""
    if uses_calendar_booking(mode):
        return system_prompt
    block = registration_guidance(
        registration_url=registration_url,
        cta_label=cta_label,
        cta_instruction=cta_instruction,
    )
    prompt = system_prompt.rstrip()
    return f"{prompt}\n\n{block}" if prompt else block


__all__ = [
    "ConversionMode",
    "DEFAULT_CONVERSION_MODE",
    "parse_conversion_mode",
    "registration_guidance",
    "uses_calendar_booking",
    "with_conversion_guidance",
]
