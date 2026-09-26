"""Meta WhatsApp Cloud API provider (specification §8, §20, [D6]).

The real implementation behind `WhatsAppProvider`. Nothing above `app.integrations`
imports this module; it is selected by configuration.

Failure classification (§20) is the load-bearing part. Meta returns the same shape for
problems that are worth retrying and problems that never will be, so mapping them
correctly is what stops the worker either giving up on a transient blip or hammering
Meta with a request that cannot succeed.

Access tokens are never logged (§19). Errors carry Meta's code and message, which do
not contain the token.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.errors import NonRetryableProviderError, RetryableProviderError
from app.core.logging import get_logger
from app.domain.enums import MessageStatus, TemplateCategory, TemplateHeaderType
from app.integrations.whatsapp.provider import (
    ApprovedTemplate,
    OutboundTemplateMessage,
    OutboundTextMessage,
    SentMessage,
    WhatsAppProvider,
)

logger = get_logger(__name__)

# Meta error codes that will never succeed on retry, whatever the HTTP status.
# 131026: the recipient cannot receive messages (not on WhatsApp, or blocked).
# 131047: outside the customer service window, so a free-form message is refused.
# 131051: unsupported message type. 132000-132015: template problems.
_NON_RETRYABLE_CODES = frozenset(
    {131026, 131047, 131051, 132000, 132001, 132005, 132007, 132012, 132015}
)

# Transient on Meta's side; the same request may well succeed shortly.
_RETRYABLE_CODES = frozenset({130429, 131048, 131056, 133016, 4, 80007})


class MetaWhatsAppProvider(WhatsAppProvider):
    """Talks to the Meta Cloud API over HTTPS."""

    name = "meta"

    def __init__(
        self,
        *,
        access_token: str,
        # None for a provider built only to read an account's templates: it has no number
        # to send from, and refuses to send rather than guessing one.
        phone_number_id: str | None,
        business_account_id: str,
        api_version: str = "v21.0",
        base_url: str = "https://graph.facebook.com",
        timeout_seconds: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._access_token = access_token
        self._phone_number_id = phone_number_id
        self._business_account_id = business_account_id
        self._base_url = f"{base_url.rstrip('/')}/{api_version}"
        self._timeout = timeout_seconds
        # Injectable so tests can supply a mock transport and never touch the network.
        self._client = client

    # ---------------------------------------------------------------- interface

    async def send_text_message(self, message: OutboundTextMessage) -> SentMessage:
        """Send a free-form message.

        Only deliverable inside the 24-hour customer service window. The window is
        enforced before this is called; Meta rejecting it here (131047) is the
        backstop, and is classified non-retryable because time will not fix it.
        """
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": message.to_e164,
            "type": "text",
            # Link previews are off: a marketing message should render as written.
            "text": {"preview_url": False, "body": message.body},
        }
        return await self._send(payload, to=message.to_e164)

    async def send_template_message(self, message: OutboundTemplateMessage) -> SentMessage:
        """Send an approved template.

        Template *content* comes from Meta; only the variable values are supplied
        here, which is why [D6] can forbid inventing template text without limiting
        what a campaign can do.
        """
        components: list[dict[str, Any]] = []

        # The header must mirror the approved template, and Meta rejects a send whose
        # components disagree with it (132012, "header: Format mismatch, expected IMAGE,
        # received UNKNOWN"). So a template with a media header is not sendable by
        # omitting the header — which is what a body-only payload does, and why the
        # campaign failed permanently on its first attempt.
        header = _header_component(message)
        if header is not None:
            components.append(header)

        if message.variables:
            components.append(
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": value} for value in message.variables],
                }
            )

        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": message.to_e164,
            "type": "template",
            "template": {
                "name": message.template_name,
                "language": {"code": message.language},
                **({"components": components} if components else {}),
            },
        }
        return await self._send(payload, to=message.to_e164)

    async def fetch_approved_templates(self) -> list[ApprovedTemplate]:
        """List templates from the WhatsApp Business Account ([D6]).

        Returns what Meta reports. Approval is never assigned here; the caller filters
        on the status this carries through.
        """
        templates: list[ApprovedTemplate] = []
        url: str | None = f"{self._base_url}/{self._business_account_id}/message_templates"
        params: dict[str, Any] | None = {"limit": 100}

        while url:
            body = await self._request("GET", url, params=params)
            for raw in body.get("data", []):
                parsed = _parse_template(raw)
                if parsed is not None:
                    templates.append(parsed)

            # Meta paginates with an absolute `next` URL that already carries its own
            # cursor, so params must not be reapplied.
            url = body.get("paging", {}).get("next")
            params = None

        return templates

    # ---------------------------------------------------------------- internals

    async def _send(self, payload: dict[str, Any], *, to: str) -> SentMessage:
        if self._phone_number_id is None:
            # Built only to read an account's templates. Sending needs a product's number,
            # and guessing one is exactly what per-product numbers exist to prevent.
            raise NonRetryableProviderError(
                "This WhatsApp provider has no phone number to send from", provider=self.name
            )
        body = await self._request(
            "POST", f"{self._base_url}/{self._phone_number_id}/messages", json=payload
        )

        messages = body.get("messages") or []
        if not messages or not messages[0].get("id"):
            # Without a provider message ID nothing downstream can correlate status
            # webhooks, so an accepted-but-unidentified send is treated as a failure.
            raise NonRetryableProviderError(
                "Meta accepted the request but returned no message ID", provider=self.name
            )

        logger.info("whatsapp_message_sent", provider_message_id=messages[0]["id"])
        # SENT, not DELIVERED: Meta has accepted it, and delivery is confirmed later
        # by a status webhook (§8).
        return SentMessage(provider_message_id=messages[0]["id"], status=MessageStatus.SENT)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._access_token}"}

        try:
            if self._client is not None:
                response = await self._client.request(
                    method, url, json=json, params=params, headers=headers, timeout=self._timeout
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(
                        method, url, json=json, params=params, headers=headers
                    )
        except httpx.TimeoutException as exc:
            # A timeout says nothing about whether Meta processed the request, so it
            # is retryable and the caller's idempotency key is what prevents a double
            # send (§20).
            raise RetryableProviderError(f"Request timed out: {exc}", provider=self.name) from exc
        except httpx.HTTPError as exc:
            raise RetryableProviderError(f"Network failure: {exc}", provider=self.name) from exc

        if response.is_success:
            try:
                parsed: dict[str, Any] = response.json()
            except ValueError as exc:
                raise NonRetryableProviderError(
                    "Meta returned a success status with an unreadable body", provider=self.name
                ) from exc
            return parsed

        raise self._classify_error(response)

    def _classify_error(self, response: httpx.Response) -> Exception:
        """Turn a Meta error response into the §20 retryable/non-retryable split."""
        code: int | None = None
        message = response.text[:500]

        try:
            error = response.json().get("error", {})
            code = error.get("code")
            message = error.get("message", message)
            if error.get("error_data", {}).get("details"):
                message = f"{message}: {error['error_data']['details']}"
        except ValueError:
            pass

        # An explicit Meta code is more reliable than the HTTP status, which is 400
        # for both "this number does not exist" and "you are being rate limited".
        if code in _NON_RETRYABLE_CODES:
            return NonRetryableProviderError(f"[{code}] {message}", provider=self.name)
        if code in _RETRYABLE_CODES:
            return RetryableProviderError(f"[{code}] {message}", provider=self.name)

        if response.status_code == 429:
            return RetryableProviderError(f"Rate limited: {message}", provider=self.name)
        if response.status_code >= 500:
            return RetryableProviderError(
                f"Meta server error {response.status_code}: {message}", provider=self.name
            )
        if response.status_code in (401, 403):
            # Retrying with the same rejected credentials only burns quota.
            return NonRetryableProviderError(
                f"Authentication failed ({response.status_code}): {message}", provider=self.name
            )
        return NonRetryableProviderError(
            f"Request rejected ({response.status_code}): {message}", provider=self.name
        )


def _header_component(message: OutboundTemplateMessage) -> dict[str, Any] | None:
    """The `header` component for a template send, or None when it needs none.

    Meta validates the sent components against the approved template, so this mirrors
    the template's header rather than choosing one.

    **Only a media header contributes anything.** A TEXT header's text is part of the
    approved template — exactly like the body text, which has never been sent either —
    so there is nothing to supply and nothing to declare. Emitting an empty `parameters`
    array for one is not merely redundant: Meta rejects a parameter object that has no
    `type`, and an empty array is the same class of mistake.

    A media header parameter carries **both** the type discriminator and the media:
    `{"type": "image", "image": {"link": ...}}`. Omitting `type` is a schema violation
    the API rejects with a 400 (observed on staging 2026-09-26, campaign
    `3c8bbc8d-6a19-4764-b80e-7d4e268eabfe`), and it is worth stating because the body
    parameters below already carry `type` while this one originally did not — the
    omission was invisible until Meta parsed the whole request.

    A media header with no URL is refused here rather than sent. A silent omission is
    exactly the 132012 this whole path exists to prevent, and it fails permanently on
    every recipient, so it is far better raised before the request than after it.
    """
    if not message.header_type.needs_media:
        return None

    if not message.header_media_url:
        raise NonRetryableProviderError(
            f"Template {message.template_name!r} has a "
            f"{message.header_type.value} header, which needs a media URL, "
            "and none was supplied",
            provider="meta",
        )

    # Meta's parameter type is the lowercased header type: an IMAGE header takes an
    # `image` parameter, a DOCUMENT header a `document` one.
    media_key = message.header_type.value.lower()
    return {
        "type": "header",
        "parameters": [{"type": media_key, media_key: {"link": message.header_media_url}}],
    }


def _parse_template(raw: dict[str, Any]) -> ApprovedTemplate | None:
    """Convert one Meta template record.

    Returns None for a category this platform does not model, rather than guessing:
    an unrecognized category must not silently become MARKETING and be broadcast.
    """
    try:
        category = TemplateCategory(raw["category"])
    except (KeyError, ValueError):
        logger.warning("whatsapp_template_skipped", reason="unrecognized category")
        return None

    body = ""
    header_type = TemplateHeaderType.NONE
    header_sample_url: str | None = None
    for component in raw.get("components", []):
        if component.get("type") == "BODY":
            body = component.get("text", "")
        elif component.get("type") == "HEADER":
            # Recorded rather than dropped. A header that is not known here cannot be
            # reproduced at send time, and Meta refuses the send for exactly that.
            header_type = _parse_header_type(component.get("format"))
            header_sample_url = _header_sample_url(component)

    return ApprovedTemplate(
        name=raw.get("name", ""),
        language=raw.get("language", ""),
        category=category,
        provider_template_id=str(raw.get("id", "")),
        # Meta does not report a parameter count, so it is counted from the
        # placeholders in the body text.
        variable_count=body.count("{{"),
        body_preview=body or None,
        # Carried through unchanged; nothing here decides approval ([D6]).
        provider_status=raw.get("status", ""),
        header_type=header_type,
        header_sample_url=header_sample_url,
    )


def _parse_header_type(fmt: str | None) -> TemplateHeaderType:
    """Map Meta's header `format` onto a modeled type, defaulting to NONE.

    An unrecognized format is NONE rather than a guess. That understates the header,
    so the send is refused later for lacking media rather than claiming a shape Meta
    did not approve — the safe direction to be wrong in.
    """
    if not fmt:
        return TemplateHeaderType.NONE
    try:
        return TemplateHeaderType(fmt.upper())
    except ValueError:
        logger.warning("whatsapp_template_header_unrecognized", header_format=fmt)
        return TemplateHeaderType.NONE


def _header_sample_url(component: dict[str, Any]) -> str | None:
    """The example media link Meta stored for a media header, if it has one.

    Reference only. It is never sent: the example attached to an approved template is
    a sample shown in the manager, not the asset a campaign is meant to deliver.
    """
    for value in component.get("example", {}).values():
        if isinstance(value, dict):
            link = value.get("link")
            if isinstance(link, str):
                return link
    return None
