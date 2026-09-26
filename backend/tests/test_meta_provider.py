"""Meta provider tests (§8, §19, §20, [D6]).

Every request is served by an in-memory mock transport. CLAUDE.md forbids tests
sending real WhatsApp messages, and this is how that is guaranteed rather than
merely intended: no socket is ever opened.

The failure classification is the focus. Meta returns similar shapes for problems
worth retrying and problems that never will be, and getting the split wrong means
either abandoning a transient blip or hammering Meta with a doomed request.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.core.errors import NonRetryableProviderError, RetryableProviderError
from app.domain.enums import MessageStatus, TemplateCategory, TemplateHeaderType
from app.integrations.whatsapp.meta import MetaWhatsAppProvider
from app.integrations.whatsapp.provider import OutboundTemplateMessage, OutboundTextMessage

PHONE = "+919876543210"


def make_provider(handler: Any) -> MetaWhatsAppProvider:
    transport = httpx.MockTransport(handler)
    return MetaWhatsAppProvider(
        access_token="secret-token",  # noqa: S106 - never leaves the mock
        phone_number_id="phone-1",
        business_account_id="waba-1",
        client=httpx.AsyncClient(transport=transport),
    )


def ok_send(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"messages": [{"id": "wamid.ABC"}]})


def error(status_code: int, code: int | None = None, message: str = "boom") -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = {"error": {"message": message}}
        if code is not None:
            body["error"]["code"] = code
        return httpx.Response(status_code, json=body)

    return handler


class TestSending:
    async def test_a_text_message_returns_the_provider_id(self) -> None:
        result = await make_provider(ok_send).send_text_message(
            OutboundTextMessage(to_e164=PHONE, body="Hello")
        )
        assert result.provider_message_id == "wamid.ABC"

    async def test_status_is_sent_not_delivered(self) -> None:
        """Meta has only accepted the message; delivery is confirmed later by a
        status webhook (§8)."""
        result = await make_provider(ok_send).send_text_message(
            OutboundTextMessage(to_e164=PHONE, body="Hello")
        )
        assert result.status is MessageStatus.SENT

    async def test_the_text_payload_shape(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured.update(json.loads(request.content))
            return ok_send(request)

        await make_provider(handler).send_text_message(
            OutboundTextMessage(to_e164=PHONE, body="Hello")
        )

        assert captured["messaging_product"] == "whatsapp"
        assert captured["to"] == PHONE
        assert captured["type"] == "text"
        assert captured["text"]["body"] == "Hello"
        # A marketing message should render as written, not as a link card.
        assert captured["text"]["preview_url"] is False

    async def test_the_template_payload_carries_variables(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured.update(json.loads(request.content))
            return ok_send(request)

        await make_provider(handler).send_template_message(
            OutboundTemplateMessage(
                to_e164=PHONE, template_name="q3_promo", language="en", variables=("Rahul",)
            )
        )

        assert captured["type"] == "template"
        assert captured["template"]["name"] == "q3_promo"
        assert captured["template"]["language"]["code"] == "en"
        assert captured["template"]["components"][0]["parameters"][0]["text"] == "Rahul"

    async def test_a_template_without_variables_omits_components(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured.update(json.loads(request.content))
            return ok_send(request)

        await make_provider(handler).send_template_message(
            OutboundTemplateMessage(to_e164=PHONE, template_name="simple", language="en")
        )
        assert "components" not in captured["template"]

    async def test_an_image_header_is_sent_as_a_header_component(self) -> None:
        """The regression behind staging's 132012.

        An approved template with an IMAGE header must be sent with a matching header
        component. Omitting it made Meta answer "header: Format mismatch, expected
        IMAGE, received UNKNOWN" and fail the recipient permanently.
        """
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured.update(json.loads(request.content))
            return ok_send(request)

        await make_provider(handler).send_template_message(
            OutboundTemplateMessage(
                to_e164=PHONE,
                template_name="sutainx_test",
                language="en",
                header_type=TemplateHeaderType.IMAGE,
                header_media_url="https://cdn.example.com/sutainx.jpg",
            )
        )

        header = captured["template"]["components"][0]
        assert header["type"] == "header"
        assert header["parameters"][0]["image"]["link"] == "https://cdn.example.com/sutainx.jpg"

    async def test_the_header_precedes_the_body(self) -> None:
        """Order matters to Meta: header first, then body."""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured.update(json.loads(request.content))
            return ok_send(request)

        await make_provider(handler).send_template_message(
            OutboundTemplateMessage(
                to_e164=PHONE,
                template_name="sutainx_test",
                language="en",
                variables=("Rahul",),
                header_type=TemplateHeaderType.IMAGE,
                header_media_url="https://cdn.example.com/sutainx.jpg",
            )
        )

        components = captured["template"]["components"]
        assert [c["type"] for c in components] == ["header", "body"]

    async def test_a_media_header_without_a_url_is_refused_before_the_request(self) -> None:
        """Never sent silently.

        A missing asset cannot be fixed by retrying, so it is raised here rather than
        spending an attempt per recipient to be refused permanently by Meta.
        """
        called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return ok_send(request)

        with pytest.raises(NonRetryableProviderError, match="needs a media URL"):
            await make_provider(handler).send_template_message(
                OutboundTemplateMessage(
                    to_e164=PHONE,
                    template_name="sutainx_test",
                    language="en",
                    header_type=TemplateHeaderType.IMAGE,
                )
            )
        assert called is False

    async def test_a_document_header_uses_the_document_parameter(self) -> None:
        """Meta's parameter key is the lowercased header type."""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured.update(json.loads(request.content))
            return ok_send(request)

        await make_provider(handler).send_template_message(
            OutboundTemplateMessage(
                to_e164=PHONE,
                template_name="menu",
                language="en",
                header_type=TemplateHeaderType.DOCUMENT,
                header_media_url="https://cdn.example.com/menu.pdf",
            )
        )

        header = captured["template"]["components"][0]
        assert "document" in header["parameters"][0]

    async def test_a_text_header_needs_no_media(self) -> None:
        """A TEXT header carries the template's own text, so nothing is required."""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured.update(json.loads(request.content))
            return ok_send(request)

        await make_provider(handler).send_template_message(
            OutboundTemplateMessage(
                to_e164=PHONE,
                template_name="greeting",
                language="en",
                header_type=TemplateHeaderType.TEXT,
            )
        )

        assert captured["template"]["components"][0]["type"] == "header"

    async def test_the_access_token_is_sent_as_a_bearer_header(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("Authorization", "")
            return ok_send(request)

        await make_provider(handler).send_text_message(
            OutboundTextMessage(to_e164=PHONE, body="Hello")
        )
        assert seen["auth"] == "Bearer secret-token"

    async def test_a_success_without_a_message_id_is_a_failure(self) -> None:
        """Without a provider ID nothing can correlate the status webhooks, so an
        accepted-but-unidentified send is not a success."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"messages": []})

        with pytest.raises(NonRetryableProviderError, match="no message ID"):
            await make_provider(handler).send_text_message(
                OutboundTextMessage(to_e164=PHONE, body="Hello")
            )


class TestRetryableFailures:
    """§20: retry only what might succeed."""

    async def test_a_timeout_is_retryable(self) -> None:
        """A timeout says nothing about whether Meta processed it; the idempotency
        key is what prevents a double send."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        with pytest.raises(RetryableProviderError, match="timed out"):
            await make_provider(handler).send_text_message(
                OutboundTextMessage(to_e164=PHONE, body="Hello")
            )

    async def test_a_network_error_is_retryable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        with pytest.raises(RetryableProviderError, match="Network failure"):
            await make_provider(handler).send_text_message(
                OutboundTextMessage(to_e164=PHONE, body="Hello")
            )

    async def test_rate_limiting_is_retryable(self) -> None:
        with pytest.raises(RetryableProviderError):
            await make_provider(error(429)).send_text_message(
                OutboundTextMessage(to_e164=PHONE, body="Hello")
            )

    @pytest.mark.parametrize("status_code", [500, 502, 503])
    async def test_server_errors_are_retryable(self, status_code: int) -> None:
        with pytest.raises(RetryableProviderError):
            await make_provider(error(status_code)).send_text_message(
                OutboundTextMessage(to_e164=PHONE, body="Hello")
            )

    async def test_a_meta_throttle_code_is_retryable_despite_a_400(self) -> None:
        """Meta returns 400 for throttling as well as for permanent problems, so the
        error code is more reliable than the status."""
        with pytest.raises(RetryableProviderError):
            await make_provider(error(400, code=130429)).send_text_message(
                OutboundTextMessage(to_e164=PHONE, body="Hello")
            )


class TestNonRetryableFailures:
    @pytest.mark.parametrize("status_code", [401, 403])
    async def test_authentication_failures_are_not_retried(self, status_code: int) -> None:
        """Retrying with the same rejected credentials only burns quota."""
        with pytest.raises(NonRetryableProviderError, match="Authentication failed"):
            await make_provider(error(status_code)).send_text_message(
                OutboundTextMessage(to_e164=PHONE, body="Hello")
            )

    async def test_an_undeliverable_recipient_is_not_retried(self) -> None:
        with pytest.raises(NonRetryableProviderError):
            await make_provider(error(400, code=131026)).send_text_message(
                OutboundTextMessage(to_e164=PHONE, body="Hello")
            )

    async def test_being_outside_the_service_window_is_not_retried(self) -> None:
        """131047 means the 24-hour window has closed. Time will not fix it; only a
        template send will."""
        with pytest.raises(NonRetryableProviderError):
            await make_provider(error(400, code=131047)).send_text_message(
                OutboundTextMessage(to_e164=PHONE, body="Hello")
            )

    async def test_the_error_message_is_preserved(self) -> None:
        with pytest.raises(NonRetryableProviderError, match="Recipient not found"):
            await make_provider(
                error(400, code=131026, message="Recipient not found")
            ).send_text_message(OutboundTextMessage(to_e164=PHONE, body="Hello"))

    async def test_the_access_token_never_appears_in_an_error(self) -> None:
        """§19: never log provider access tokens."""
        with pytest.raises(NonRetryableProviderError) as exc_info:
            await make_provider(error(400, message="bad request")).send_text_message(
                OutboundTextMessage(to_e164=PHONE, body="Hello")
            )
        assert "secret-token" not in str(exc_info.value)


class TestTemplateSync:
    def _templates_handler(self, pages: list[dict[str, Any]]) -> Any:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            page = pages[min(calls["n"], len(pages) - 1)]
            calls["n"] += 1
            return httpx.Response(200, json=page)

        return handler

    async def test_templates_are_parsed(self) -> None:
        handler = self._templates_handler(
            [
                {
                    "data": [
                        {
                            "id": "tpl-1",
                            "name": "q3_promo",
                            "language": "en",
                            "status": "APPROVED",
                            "category": "MARKETING",
                            "components": [
                                {"type": "BODY", "text": "Hi {{1}}, our {{2}} offer is live."}
                            ],
                        }
                    ]
                }
            ]
        )
        templates = await make_provider(handler).fetch_approved_templates()

        assert len(templates) == 1
        assert templates[0].name == "q3_promo"
        assert templates[0].category is TemplateCategory.MARKETING
        assert templates[0].variable_count == 2

    async def test_an_image_header_is_recorded_on_sync(self) -> None:
        """The other half of the 132012 fix: the sync must learn the shape.

        A header that is not read here cannot be reproduced at send time, so Meta
        refuses the send for exactly that reason.
        """
        handler = self._templates_handler(
            [
                {
                    "data": [
                        {
                            "id": "tpl-3",
                            "name": "sutainx_test",
                            "language": "en",
                            "status": "APPROVED",
                            "category": "MARKETING",
                            "components": [
                                {"type": "HEADER", "format": "IMAGE"},
                                {"type": "BODY", "text": "Hi {{1}}"},
                            ],
                        }
                    ]
                }
            ]
        )
        templates = await make_provider(handler).fetch_approved_templates()
        assert templates[0].header_type is TemplateHeaderType.IMAGE
        # The body is still parsed; the header did not displace it.
        assert templates[0].variable_count == 1
        assert templates[0].body_preview == "Hi {{1}}"

    async def test_the_samples_image_is_kept_for_reference_only(self) -> None:
        """Meta's example is recorded, and is clearly not treated as the asset.

        It is a placeholder in the manager, so it must never become the media a
        campaign sends — hence it is exposed as `header_sample_url`, a name the send
        path never reads.
        """
        handler = self._templates_handler(
            [
                {
                    "data": [
                        {
                            "id": "tpl-4",
                            "name": "with_sample",
                            "language": "en",
                            "status": "APPROVED",
                            "category": "MARKETING",
                            "components": [
                                {
                                    "type": "HEADER",
                                    "format": "IMAGE",
                                    "example": {
                                        "header_handle": ["https://cdn.example.com/sample.jpg"]
                                    },
                                }
                            ],
                        }
                    ]
                }
            ]
        )
        templates = await make_provider(handler).fetch_approved_templates()
        assert templates[0].header_sample_url is None  # a list, not a link object

    async def test_a_body_only_template_has_no_header(self) -> None:
        """The default, and the reason existing rows need no backfill."""
        handler = self._templates_handler(
            [
                {
                    "data": [
                        {
                            "id": "tpl-5",
                            "name": "plain",
                            "language": "en",
                            "status": "APPROVED",
                            "category": "MARKETING",
                            "components": [{"type": "BODY", "text": "Hello"}],
                        }
                    ]
                }
            ]
        )
        templates = await make_provider(handler).fetch_approved_templates()
        assert templates[0].header_type is TemplateHeaderType.NONE

    async def test_an_unrecognized_header_format_is_not_guessed(self) -> None:
        """NONE understates the header, which fails loudly later.

        Guessing a type we do not model would claim a shape Meta never approved. NONE
        instead makes the send refuse for missing media, naming the real problem.
        """
        handler = self._templates_handler(
            [
                {
                    "data": [
                        {
                            "id": "tpl-6",
                            "name": "exotic",
                            "language": "en",
                            "status": "APPROVED",
                            "category": "MARKETING",
                            "components": [{"type": "HEADER", "format": "STICKER"}],
                        }
                    ]
                }
            ]
        )
        templates = await make_provider(handler).fetch_approved_templates()
        assert templates[0].header_type is TemplateHeaderType.NONE

    async def test_metas_status_is_carried_through_unchanged(self) -> None:
        """[D6]: approval is Meta's. The sync records what it was told, including a
        PENDING template, and callers filter on it."""
        handler = self._templates_handler(
            [
                {
                    "data": [
                        {
                            "id": "tpl-2",
                            "name": "pending_one",
                            "language": "en",
                            "status": "PENDING",
                            "category": "MARKETING",
                            "components": [],
                        }
                    ]
                }
            ]
        )
        templates = await make_provider(handler).fetch_approved_templates()
        assert templates[0].provider_status == "PENDING"

    async def test_an_unrecognized_category_is_skipped(self) -> None:
        """It must not silently default to MARKETING and become broadcastable."""
        handler = self._templates_handler(
            [{"data": [{"id": "t", "name": "x", "language": "en", "category": "NEW_KIND"}]}]
        )
        assert await make_provider(handler).fetch_approved_templates() == []

    async def test_pagination_is_followed(self) -> None:
        pages: list[dict[str, Any]] = [
            {
                "data": [
                    {
                        "id": "tpl-1",
                        "name": "first",
                        "language": "en",
                        "status": "APPROVED",
                        "category": "MARKETING",
                    }
                ],
                "paging": {"next": "https://graph.facebook.com/v21.0/next-page"},
            },
            {
                "data": [
                    {
                        "id": "tpl-2",
                        "name": "second",
                        "language": "en",
                        "status": "APPROVED",
                        "category": "UTILITY",
                    }
                ]
            },
        ]
        templates = await make_provider(self._templates_handler(pages)).fetch_approved_templates()
        assert {t.name for t in templates} == {"first", "second"}

    async def test_an_empty_account_returns_nothing(self) -> None:
        handler = self._templates_handler([{"data": []}])
        assert await make_provider(handler).fetch_approved_templates() == []
