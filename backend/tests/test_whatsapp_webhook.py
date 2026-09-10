"""Webhook parsing, signature and messaging-window tests (§8, §19, [D5])."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest

from app.domain.enums import MessageStatus
from app.domain.whatsapp_webhook import (
    is_within_service_window,
    matches_opt_out,
    parse_webhook,
    verify_signature,
    verify_subscription,
)

APP_SECRET = "test-app-secret"  # noqa: S105 - test fixture
OTHER_SECRET = "different-secret"  # noqa: S105 - test fixture
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
PHONE_NUMBER_ID = "108000000000001"


def signed(payload: dict[str, object]) -> tuple[bytes, str]:
    body = json.dumps(payload).encode()
    digest = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return body, f"sha256={digest}"


def inbound_payload(
    text: str = "Hello",
    message_id: str = "wamid.1",
    *,
    phone_number_id: str | None = PHONE_NUMBER_ID,
) -> dict[str, object]:
    metadata: dict[str, object] = {"display_phone_number": "911140001234"}
    if phone_number_id is not None:
        metadata["phone_number_id"] = phone_number_id

    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": metadata,
                            "contacts": [{"wa_id": "919876543210", "profile": {"name": "Rahul"}}],
                            "messages": [
                                {
                                    "id": message_id,
                                    "from": "919876543210",
                                    "timestamp": "1788000000",
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def status_payload(status: str = "delivered", message_id: str = "wamid.1") -> dict[str, object]:
    entry: dict[str, object] = {
        "id": message_id,
        "status": status,
        "timestamp": "1788000100",
        "recipient_id": "919876543210",
    }
    if status == "failed":
        entry["errors"] = [{"code": 131026, "title": "Message undeliverable"}]

    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
                            "statuses": [entry],
                        },
                    }
                ],
            }
        ],
    }


class TestSignatureVerification:
    def test_a_valid_signature_is_accepted(self) -> None:
        body, header = signed(inbound_payload())
        assert verify_signature(payload=body, signature_header=header, app_secret=APP_SECRET)

    def test_a_tampered_body_is_rejected(self) -> None:
        """The whole point: an attacker must not be able to fabricate a customer
        reply or an opt-out."""
        _, header = signed(inbound_payload())
        assert not verify_signature(
            payload=b'{"entry": []}', signature_header=header, app_secret=APP_SECRET
        )

    def test_a_wrong_secret_is_rejected(self) -> None:
        body, header = signed(inbound_payload())
        assert not verify_signature(
            payload=body,
            signature_header=header,
            app_secret="different-secret",  # noqa: S106 - test fixture
        )

    @pytest.mark.parametrize("header", [None, "", "abc123", "sha1=abc", "sha256="])
    def test_malformed_headers_are_rejected(self, header: str | None) -> None:
        body, _ = signed(inbound_payload())
        assert not verify_signature(payload=body, signature_header=header, app_secret=APP_SECRET)


class TestSubscriptionVerification:
    def test_a_correct_handshake_returns_the_challenge(self) -> None:
        assert (
            verify_subscription(
                mode="subscribe", token="tok", challenge="12345", verify_token="tok"
            )
            == "12345"
        )

    @pytest.mark.parametrize(
        ("mode", "token"),
        [("subscribe", "wrong"), ("unsubscribe", "tok"), (None, "tok"), ("subscribe", None)],
    )
    def test_bad_handshakes_are_rejected(self, mode: str | None, token: str | None) -> None:
        assert (
            verify_subscription(mode=mode, token=token, challenge="12345", verify_token="tok")
            is None
        )


class TestInboundParsing:
    def test_a_text_message_is_extracted(self) -> None:
        parsed = parse_webhook(inbound_payload("Tell me more"))

        assert len(parsed.inbound) == 1
        message = parsed.inbound[0]
        assert message.text == "Tell me more"
        assert message.provider_message_id == "wamid.1"
        assert message.profile_name == "Rahul"

    def test_the_sender_is_normalized_to_e164(self) -> None:
        """Meta reports wa_id without a '+'; the platform's identity is E.164."""
        assert parse_webhook(inbound_payload()).inbound[0].from_e164 == "+919876543210"

    def test_the_timestamp_is_timezone_aware(self) -> None:
        assert parse_webhook(inbound_payload()).inbound[0].timestamp.tzinfo is not None

    def test_a_button_reply_carries_its_label(self) -> None:
        """A quick-reply tap must count the same as typing the word, or an opt-out
        button would be silently ignored."""
        payload = inbound_payload()
        message = payload["entry"][0]["changes"][0]["value"]["messages"][0]  # type: ignore[index]
        message["type"] = "button"
        message["button"] = {"text": "STOP"}
        del message["text"]

        assert parse_webhook(payload).inbound[0].text == "STOP"

    def test_an_interactive_reply_carries_its_title(self) -> None:
        payload = inbound_payload()
        message = payload["entry"][0]["changes"][0]["value"]["messages"][0]  # type: ignore[index]
        message["type"] = "interactive"
        message["interactive"] = {"button_reply": {"id": "b1", "title": "Book a demo"}}
        del message["text"]

        assert parse_webhook(payload).inbound[0].text == "Book a demo"

    def test_a_media_message_is_kept_without_text(self) -> None:
        """An image is still a customer contact; it opens the reply window even
        though there is nothing to read."""
        payload = inbound_payload()
        message = payload["entry"][0]["changes"][0]["value"]["messages"][0]  # type: ignore[index]
        message["type"] = "image"
        del message["text"]

        parsed = parse_webhook(payload).inbound[0]
        assert parsed.text is None
        assert parsed.message_type == "image"

    def test_several_messages_in_one_delivery(self) -> None:
        """Meta batches, so one POST can carry several messages."""
        payload = inbound_payload()
        messages = payload["entry"][0]["changes"][0]["value"]["messages"]  # type: ignore[index]
        messages.append(
            {
                "id": "wamid.2",
                "from": "919876543211",
                "timestamp": "1788000001",
                "type": "text",
                "text": {"body": "Second"},
            }
        )
        assert len(parse_webhook(payload).inbound) == 2


class TestReceivingNumber:
    """The routing key (Phase 23, [D40]).

    With one Meta App and one shared WABA, every product's messages arrive through the
    same webhook and the same app secret. `metadata.phone_number_id` — the number the
    customer wrote *to* — is the only field that differs between them, so the parser has
    to carry it through; the entry `id` is the WABA, which is shared and identifies
    nothing.
    """

    def test_the_receiving_number_is_carried_through(self) -> None:
        assert parse_webhook(inbound_payload()).inbound[0].phone_number_id == PHONE_NUMBER_ID

    def test_every_message_in_a_batch_gets_it(self) -> None:
        """One delivery, one receiving number — but the field is per message, so a
        batch must not leave later ones unrouted."""
        payload = inbound_payload()
        messages = payload["entry"][0]["changes"][0]["value"]["messages"]  # type: ignore[index]
        messages.append(
            {
                "id": "wamid.2",
                "from": "919876543211",
                "timestamp": "1788000001",
                "type": "text",
                "text": {"body": "Second"},
            }
        )
        assert [m.phone_number_id for m in parse_webhook(payload).inbound] == [
            PHONE_NUMBER_ID,
            PHONE_NUMBER_ID,
        ]

    def test_a_status_update_carries_it_too(self) -> None:
        assert parse_webhook(status_payload()).statuses[0].phone_number_id == PHONE_NUMBER_ID

    def test_a_missing_number_parses_to_none_rather_than_raising(self) -> None:
        """§8 still applies: an odd delivery must not become a non-2xx and a redelivery
        loop. The parser reports the absence and the worker refuses to act on it."""
        parsed = parse_webhook(inbound_payload(phone_number_id=None))

        assert len(parsed.inbound) == 1
        assert parsed.inbound[0].phone_number_id is None

    @pytest.mark.parametrize("metadata", ["not-a-dict", None, {}, {"phone_number_id": ""}])
    def test_unusable_metadata_gives_none(self, metadata: object) -> None:
        payload = inbound_payload()
        payload["entry"][0]["changes"][0]["value"]["metadata"] = metadata  # type: ignore[index]

        assert parse_webhook(payload).inbound[0].phone_number_id is None

    def test_a_numeric_number_is_normalized_to_a_string(self) -> None:
        """Meta sends it as a string, but JSON is JSON and the comparison against the
        stored configuration is textual — a stray int must not silently fail to match."""
        payload = inbound_payload()
        payload["entry"][0]["changes"][0]["value"]["metadata"] = {  # type: ignore[index]
            "phone_number_id": 108000000000001
        }

        assert parse_webhook(payload).inbound[0].phone_number_id == PHONE_NUMBER_ID


class TestStatusParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("sent", MessageStatus.SENT),
            ("delivered", MessageStatus.DELIVERED),
            ("read", MessageStatus.READ),
            ("failed", MessageStatus.FAILED),
        ],
    )
    def test_statuses_map_to_the_specified_set(self, raw: str, expected: MessageStatus) -> None:
        """§8 defines the status set; Meta's strings map onto it."""
        assert parse_webhook(status_payload(raw)).statuses[0].status is expected

    def test_a_failure_carries_its_reason(self) -> None:
        failure = parse_webhook(status_payload("failed")).statuses[0]
        assert failure.error_code == "131026"
        assert failure.error_message == "Message undeliverable"

    def test_an_unknown_status_is_ignored(self) -> None:
        """Meta adds status values; an unrecognized one must not fail the delivery."""
        assert parse_webhook(status_payload("deleted")).statuses == []


class TestMalformedPayloads:
    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"entry": []},
            {"entry": "not-a-list"},
            {"entry": [{"changes": None}]},
            {"entry": [{"changes": [{"value": "not-a-dict"}]}]},
            {"entry": [{"changes": [{"value": {"messages": [{"no_id": True}]}}]}]},
        ],
    )
    def test_nothing_raises(self, payload: dict[str, object]) -> None:
        """§8 requires webhooks to tolerate anything. A parse error would become a
        non-2xx, and Meta would redeliver forever."""
        assert parse_webhook(payload).is_empty

    def test_a_status_only_payload_yields_no_inbound(self) -> None:
        parsed = parse_webhook(status_payload())
        assert parsed.inbound == []
        assert len(parsed.statuses) == 1


class TestOptOutMatching:
    @pytest.mark.parametrize("text", ["STOP", "stop", "  Stop  ", "STOP.", "stop!"])
    def test_opt_out_keywords_match(self, text: str) -> None:
        """[D5]: punctuation and case must not defeat an opt-out."""
        assert matches_opt_out(text, ["STOP", "UNSUBSCRIBE"])

    def test_multi_word_keywords_match(self) -> None:
        assert matches_opt_out("opt out", ["OPT OUT"])

    @pytest.mark.parametrize(
        "text",
        [
            "please don't stop sending these",
            "I want to stop by your office",
            "stopwatch",
            "",
            None,
        ],
    )
    def test_incidental_mentions_do_not_opt_out(self, text: str | None) -> None:
        """Matched on the whole message, not as a substring: unsubscribing someone who
        said 'don't stop' would be worse than missing an opt-out."""
        assert not matches_opt_out(text, ["STOP", "UNSUBSCRIBE"])

    def test_keywords_are_configurable(self) -> None:
        """[D5]: the list is configuration, so a regional term works without code."""
        assert matches_opt_out("band karo", ["BAND KARO"])
        assert not matches_opt_out("band karo", ["STOP"])

    def test_an_empty_keyword_list_never_matches(self) -> None:
        assert not matches_opt_out("STOP", [])


class TestServiceWindow:
    def test_a_recent_message_is_inside_the_window(self) -> None:
        assert is_within_service_window(NOW - timedelta(hours=1), window_hours=24, now=NOW)

    def test_an_old_message_is_outside(self) -> None:
        """Meta refuses free-form sends past the window, so the platform must know
        before it tries."""
        assert not is_within_service_window(NOW - timedelta(hours=25), window_hours=24, now=NOW)

    def test_the_boundary_is_closed(self) -> None:
        assert not is_within_service_window(NOW - timedelta(hours=24), window_hours=24, now=NOW)

    def test_no_prior_message_means_no_window(self) -> None:
        """A window only opens when the customer writes first."""
        assert not is_within_service_window(None, window_hours=24, now=NOW)

    def test_the_window_length_is_configurable(self) -> None:
        assert is_within_service_window(NOW - timedelta(hours=25), window_hours=48, now=NOW)
