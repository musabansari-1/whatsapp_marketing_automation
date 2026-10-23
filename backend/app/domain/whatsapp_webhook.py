"""WhatsApp webhook parsing and messaging rules (specification §8, [D5], [D16]).

Pure functions over the payload Meta posts. Parsing is separated from persistence so
the shape of Meta's deeply nested envelope can be tested directly, and so a payload
this platform does not model is ignored rather than crashing the webhook — §8 requires
webhooks to return quickly and tolerate anything.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from app.domain.enums import MessageStatus


class WebhookEventKind(StrEnum):
    INBOUND_MESSAGE = "inbound_message"
    STATUS_UPDATE = "status_update"


@dataclass(frozen=True, slots=True)
class InboundMessageEvent:
    """A customer message.

    `phone_number_id` is the business number that received it, and since Phase 23 it is
    the authoritative product routing key ([D40]). Optional on the dataclass because a
    malformed payload may omit it; the routing layer refuses such an event rather than
    defaulting.
    """

    provider_message_id: str
    from_e164: str
    timestamp: datetime
    text: str | None
    message_type: str
    profile_name: str | None = None
    phone_number_id: str | None = None


@dataclass(frozen=True, slots=True)
class StatusUpdateEvent:
    """A delivery, read or failure notification for a message we sent (§8)."""

    provider_message_id: str
    status: MessageStatus
    timestamp: datetime
    recipient_e164: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    # Retained for diagnostics. Status events are matched by `provider_message_id`,
    # which is globally unique, so this is never the routing key for them.
    phone_number_id: str | None = None


@dataclass(slots=True)
class ParsedWebhook:
    """Everything actionable in one delivery.

    Meta batches multiple messages and statuses into a single POST, so one webhook can
    produce several of each — and, since each `change` carries its own metadata,
    potentially for **different receiving numbers**. Each event therefore carries its
    own `phone_number_id` rather than the delivery carrying one for all of them.
    """

    inbound: list[InboundMessageEvent] = field(default_factory=list)
    statuses: list[StatusUpdateEvent] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.inbound and not self.statuses


# Meta's status strings, mapped onto the §8 statuses.
_STATUS_MAP = {
    "sent": MessageStatus.SENT,
    "delivered": MessageStatus.DELIVERED,
    "read": MessageStatus.READ,
    "failed": MessageStatus.FAILED,
}


def verify_signature(*, payload: bytes, signature_header: str | None, app_secret: str) -> bool:
    """Verify Meta's `X-Hub-Signature-256` header (§19).

    Without this the endpoint accepts forged inbound messages from anyone who knows
    the URL, which would let an attacker fabricate customer replies and opt-outs.

    Compared in constant time, and computed over the raw body — re-serializing the
    parsed JSON would change the bytes and break the digest.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected = hmac.new(app_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.removeprefix("sha256="))


def verify_subscription(
    *, mode: str | None, token: str | None, challenge: str | None, verify_token: str
) -> str | None:
    """Answer Meta's subscription handshake (§8).

    Returns the challenge to echo, or None to reject. Meta calls this once when the
    webhook is configured, and rejects the endpoint unless the challenge comes back
    verbatim.
    """
    if mode != "subscribe" or challenge is None:
        return None
    if not token or not hmac.compare_digest(token, verify_token):
        return None
    return challenge


def parse_webhook(payload: dict[str, Any]) -> ParsedWebhook:
    """Extract inbound messages and status updates from Meta's envelope.

    Unknown structures are skipped rather than raised on: Meta adds fields and event
    types over time, and a webhook that 500s on something unrecognized would be
    retried forever.
    """
    parsed = ParsedWebhook()

    for entry in _as_list(payload.get("entry")):
        for change in _as_list(entry.get("changes")):
            value = change.get("value")
            if not isinstance(value, dict):
                continue

            contacts = _profile_names(_as_list(value.get("contacts")))

            # The business number that received this change. Meta puts it in
            # `value.metadata.phone_number_id`, and Phase 23 makes it the product
            # routing key ([D40]) — before that it was parsed away and lost.
            metadata = value.get("metadata")
            phone_number_id = (
                str(metadata["phone_number_id"])
                if isinstance(metadata, dict) and metadata.get("phone_number_id")
                else None
            )

            for raw in _as_list(value.get("messages")):
                message = _parse_message(raw, contacts)
                if message is not None:
                    parsed.inbound.append(replace(message, phone_number_id=phone_number_id))

            for raw in _as_list(value.get("statuses")):
                status = _parse_status(raw)
                if status is not None:
                    parsed.statuses.append(replace(status, phone_number_id=phone_number_id))

    return parsed


# Media that can carry a caption; the caption is read as the message text.
CAPTIONED_MEDIA_TYPES = frozenset({"image", "video", "document"})
# Media a customer sends expecting an answer. Without a caption these get the fixed
# "please type your question" reply; stickers, reactions and locations get nothing.
REPLYABLE_MEDIA_TYPES = frozenset({"audio", "voice", "image", "video", "document"})


def matches_opt_out(text: str | None, keywords: Sequence[str]) -> bool:
    """Whether a customer message is an opt-out ([D5]).

    Matched on the whole normalized message rather than as a substring: "stop" must
    opt someone out, while "please don't stop sending these" must not. Punctuation is
    trimmed because "STOP." and "STOP!" are plainly the same intent.
    """
    if not text:
        return False

    normalized = " ".join(text.strip().casefold().split()).strip(".!?,;:")
    if not normalized:
        return False

    return any(normalized == keyword.strip().casefold() for keyword in keywords if keyword.strip())


def is_within_service_window(
    last_inbound_at: datetime | None, *, window_hours: int, now: datetime | None = None
) -> bool:
    """Whether a free-form reply is currently permitted.

    Meta only allows non-template messages within 24 hours of the customer's last
    inbound message. Outside it, a free-form send is rejected by Meta, so the platform
    must either use an approved template or not send.

    No prior inbound message means no window has ever opened.
    """
    if last_inbound_at is None:
        return False
    moment = now or datetime.now(UTC)
    return moment - last_inbound_at < timedelta(hours=window_hours)


# ---------------------------------------------------------------- internals


def _as_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _profile_names(contacts: Iterable[dict[str, Any]]) -> dict[str, str]:
    names: dict[str, str] = {}
    for contact in contacts:
        wa_id = contact.get("wa_id")
        name = contact.get("profile", {}).get("name")
        if isinstance(wa_id, str) and isinstance(name, str):
            names[wa_id] = name
    return names


def _parse_message(
    raw: dict[str, Any], profile_names: dict[str, str]
) -> InboundMessageEvent | None:
    message_id = raw.get("id")
    sender = raw.get("from")
    if not isinstance(message_id, str) or not isinstance(sender, str):
        return None

    message_type = str(raw.get("type", "unknown"))
    text: str | None = None
    if message_type == "text":
        text = raw.get("text", {}).get("body")
    elif message_type in {"button", "interactive"}:
        # A quick-reply tap carries its label rather than typed text, and an opt-out
        # button press must count the same as typing STOP.
        text = _interactive_text(raw, message_type)
    elif message_type in CAPTIONED_MEDIA_TYPES:
        # A photo sent with "is this medicine available?" is a question: the caption
        # is the customer's text, and answering it beats asking them to type it again.
        caption = raw.get(message_type, {}).get("caption")
        text = caption if isinstance(caption, str) and caption.strip() else None

    return InboundMessageEvent(
        provider_message_id=message_id,
        # Meta reports wa_id without a leading '+'; the platform's identity is E.164.
        from_e164=_to_e164(sender),
        timestamp=_to_datetime(raw.get("timestamp")),
        text=text,
        message_type=message_type,
        profile_name=profile_names.get(sender),
    )


def _interactive_text(raw: dict[str, Any], message_type: str) -> str | None:
    if message_type == "button":
        value = raw.get("button", {}).get("text")
        return value if isinstance(value, str) else None

    interactive = raw.get("interactive", {})
    for key in ("button_reply", "list_reply"):
        title = interactive.get(key, {}).get("title")
        if isinstance(title, str):
            return title
    return None


def _parse_status(raw: dict[str, Any]) -> StatusUpdateEvent | None:
    message_id = raw.get("id")
    status = _STATUS_MAP.get(str(raw.get("status", "")).lower())
    if not isinstance(message_id, str) or status is None:
        return None

    error_code: str | None = None
    error_message: str | None = None
    errors = _as_list(raw.get("errors"))
    if errors:
        error_code = str(errors[0].get("code", "")) or None
        error_message = errors[0].get("title") or errors[0].get("message")

    recipient = raw.get("recipient_id")
    return StatusUpdateEvent(
        provider_message_id=message_id,
        status=status,
        timestamp=_to_datetime(raw.get("timestamp")),
        recipient_e164=_to_e164(recipient) if isinstance(recipient, str) else None,
        error_code=error_code,
        error_message=error_message,
    )


def _to_e164(wa_id: str) -> str:
    cleaned = wa_id.strip()
    return cleaned if cleaned.startswith("+") else f"+{cleaned}"


def _to_datetime(value: object) -> datetime:
    """Meta sends a Unix timestamp as a string.

    Falls back to now rather than failing: a missing timestamp must not cost us the
    whole event, and the arrival time is a reasonable approximation.
    """
    try:
        return datetime.fromtimestamp(int(str(value)), tz=UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)
