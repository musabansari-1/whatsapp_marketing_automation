"""Phone normalization to E.164.

E.164 is the platform's primary WhatsApp identity ([D11], [D16]), so this is the
single place a raw spreadsheet value becomes one. Everything downstream — lead
deduplication, suppression lookups, campaign recipients — depends on two spellings of
the same number producing one string.

The default region is configuration ([D4]: Indian numbers unless the client says
otherwise), because a national-format number is meaningless without one.
"""

from __future__ import annotations

from dataclasses import dataclass

import phonenumbers


@dataclass(frozen=True, slots=True)
class PhoneResult:
    """Either a normalized number or the reason it could not be normalized."""

    e164: str | None
    error: str | None

    @property
    def ok(self) -> bool:
        return self.e164 is not None


def normalize_phone(raw: object, *, default_region: str) -> PhoneResult:
    """Normalize a spreadsheet cell to E.164.

    Accepts the shapes a spreadsheet actually produces: text, an integer Excel stored
    as a number, and a float where Excel dropped a numeric column to a double. A
    number read as a float would otherwise arrive as "919876543210.0" and fail to
    parse for a reason that looks like bad data rather than a formatting artifact.
    """
    text = _to_text(raw)
    if not text:
        return PhoneResult(None, "missing phone number")

    try:
        parsed = phonenumbers.parse(text, default_region)
    except phonenumbers.NumberParseException as exc:
        return PhoneResult(None, f"unparseable phone number ({exc.error_type})")

    if not phonenumbers.is_valid_number(parsed):
        return PhoneResult(None, "not a valid phone number")

    return PhoneResult(
        phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164), None
    )


def _to_text(raw: object) -> str:
    """Coerce a cell value to a string without inventing digits."""
    if raw is None:
        return ""
    if isinstance(raw, bool):
        # A boolean is never a phone number; treated as absent rather than "True".
        return ""
    if isinstance(raw, int):
        return str(raw)
    if isinstance(raw, float):
        # Excel stores whole numbers as floats. Keep the integer part only when the
        # value really is whole; anything else is bad data and should fail parsing.
        return str(int(raw)) if raw.is_integer() else str(raw)
    return str(raw).strip()
