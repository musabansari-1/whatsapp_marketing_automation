"""Reply hygiene: a customer is sent prose, never the tool call behind it.

The leak these cover is real model output, not a hypothetical: a model narrates the call
it is about to make, printing its name and arguments at the customer. The guard removes
that and nothing else, so most of these tests are as much about what it *keeps*.
"""

from __future__ import annotations

import pytest

from app.ai.reply_guard import clean_customer_reply

PROSE = "Thanks for reaching out! A colleague from our team will call you shortly."


class TestLeakedToolCallsAreRemoved:
    @pytest.mark.parametrize(
        "leak",
        [
            '{"name": "update_lead", "arguments": {"facts": {"interested": true}}}',
            '{"update_lead": {"facts": {"qualification_judgement": "QUALIFIED"}}}',
            '{"tool_calls": [{"id": "call-1", "name": "get_lead"}]}',
            '{"function": "book_demo", "arguments": {"start_time": "2026-09-02T10:00:00+05:30"}}',
            '{"ok": true, "recorded": true, "still_needed": ["city"]}',
            '{"lead": {"name": "Asha Rao", "status": "QUALIFIED"}}',
            '{"lead_id": "6f1c1e40-0000-4000-8000-000000000000", "city": "Pune"}',
            '[{"name": "update_lead", "arguments": {}}]',
        ],
    )
    def test_a_printed_tool_call_is_dropped(self, leak: str) -> None:
        assert clean_customer_reply(leak) == ""

    def test_prose_around_the_leak_survives(self) -> None:
        text = (
            f"{PROSE}\n\n"
            '{"name": "update_lead", "arguments": {"facts": {"interested": true}}}'
        )
        assert clean_customer_reply(text) == PROSE

    def test_a_leak_between_two_sentences_leaves_both(self) -> None:
        text = (
            "Got it, I will note that down. "
            '{"name": "update_lead", "arguments": {"facts": {"city": "Pune"}}} '
            "When would you like a call?"
        )
        assert clean_customer_reply(text) == (
            "Got it, I will note that down. When would you like a call?"
        )

    def test_a_fenced_payload_is_dropped_with_its_fence(self) -> None:
        text = f'{PROSE}\n\n```json\n{{"name": "get_conversation", "arguments": {{}}}}\n```'
        assert clean_customer_reply(text) == PROSE

    def test_single_quoted_payloads_are_recognised(self) -> None:
        # Python-style literals are what some models emit; they are not valid JSON.
        assert clean_customer_reply("{'name': 'get_lead', 'arguments': {}}") == ""

    def test_tool_call_markup_is_removed(self) -> None:
        text = (
            "Let me check that for you.\n"
            '<tool_call>{"name": "get_available_slots", "arguments": {"limit": 3}}</tool_call>'
        )
        assert clean_customer_reply(text) == "Let me check that for you."

    def test_a_qualified_key_with_no_wrapper_is_removed(self) -> None:
        assert clean_customer_reply('{"qualification_judgement": "NOT_QUALIFIED"}') == ""


class TestProseIsLeftAlone:
    def test_an_ordinary_reply_is_returned_unchanged(self) -> None:
        assert clean_customer_reply(PROSE) == PROSE

    def test_whitespace_is_only_normalised_when_something_was_removed(self) -> None:
        text = "Line one.\n\n\n\nLine two."
        assert clean_customer_reply(text) == text

    def test_the_life_herba_two_step_cta_survives(self) -> None:
        text = (
            "Download the Life Herba app from https://lifeherbagroup.com/ and then choose "
            "Join as Doctor inside the app."
        )
        assert clean_customer_reply(text) == text

    def test_json_without_tool_plumbing_inside_a_sentence_is_left_alone(self) -> None:
        # Inline only. A model quoting a value mid-sentence is not censored; the same
        # object alone on its own line is removed (see TestStandaloneJsonIsNeverSent).
        text = 'The configuration is {"city": "Pune", "radius_km": 5}.'
        assert clean_customer_reply(text) == text

    def test_braces_in_prose_are_not_mistaken_for_a_payload(self) -> None:
        text = "Use the placeholder {name} and we will fill it in."
        assert clean_customer_reply(text) == text

    def test_an_empty_reply_stays_empty(self) -> None:
        assert clean_customer_reply("") == ""

    def test_whitespace_only_stays_empty(self) -> None:
        assert clean_customer_reply("   \n\n  ") == ""

    def test_the_fallback_message_is_left_untouched(self) -> None:
        fallback = (
            "I don't have verified information on that yet. Please check "
            "lifeherbagroup.com, or call the Life Herba team on this same Life Herba "
            "number if you'd like to talk to someone."
        )
        assert clean_customer_reply(fallback) == fallback


class TestStandaloneJsonIsNeverSent:
    """A serialised blob alone in a reply is not a reply, whatever its keys are.

    This is the case marker-matching alone would miss, and it is how the guarantee is
    kept: unrecognised key names cannot smuggle a JSON payload onto WhatsApp.
    """

    def test_an_unrecognised_object_on_its_own_line_is_removed(self) -> None:
        text = 'Here are the details:\n{"city": "Pune", "radius_km": 5}\nAnything else?'
        assert clean_customer_reply(text) == "Here are the details:\n\nAnything else?"

    def test_an_unrecognised_array_on_its_own_line_is_removed(self) -> None:
        assert clean_customer_reply('["Pune", "Mumbai"]') == ""

    def test_a_pretty_printed_blob_is_removed(self) -> None:
        text = 'Sure, noted.\n{\n  "name": "get_lead",\n  "arguments": {}\n}\nAnything else?'
        assert clean_customer_reply(text) == "Sure, noted.\n\nAnything else?"

    def test_a_fenced_unrecognised_block_is_removed(self) -> None:
        text = 'Plan options:\n```json\n{"tier": "gold", "price": 500}\n```'
        assert clean_customer_reply(text) == "Plan options:"

    def test_an_empty_object_is_not_treated_as_a_payload(self) -> None:
        # `{}` carries nothing, so removing it would only mangle the sentence around it.
        text = "The placeholder is {} in our templates."
        assert clean_customer_reply(text) == text

    def test_cleaning_is_idempotent(self) -> None:
        once = clean_customer_reply('Noted!\n{"name": "update_lead", "arguments": {}}')
        assert once == "Noted!"
        assert clean_customer_reply(once) == once

    def test_a_payload_containing_a_brace_inside_a_string_still_ends_correctly(self) -> None:
        text = 'Noted. {"ok": false, "error": "Could not parse {this}"} Let me try a different way.'
        assert clean_customer_reply(text) == "Noted. Let me try a different way."

    def test_an_unterminated_payload_is_not_sent(self) -> None:
        # Truncated output is still a payload, and the guarantee is that the customer is never
        # sent one. It cannot be parsed, so it cannot be shown to be harmless — the reply fails
        # closed and the caller sends the approved fallback. Diagnosing the defect is the
        # logger's job (`ai_reply_tool_payload_removed`), not the customer's.
        text = 'Sure. {"name": "update_lead", "arguments": {"facts":'
        assert clean_customer_reply(text) == ""

    def test_a_reply_that_was_only_a_leak_and_a_tool_name_becomes_empty(self) -> None:
        # Nothing here is worth sending, so the caller's approved fallback takes over.
        text = 'update_lead:\n{"name": "update_lead", "arguments": {"facts": {}}}'
        assert clean_customer_reply(text) == ""

    def test_a_tool_name_inside_real_prose_does_not_lose_the_line(self) -> None:
        text = (
            "I could not use update_lead for this, so let me ask you directly: "
            "which city are you in?"
        )
        assert clean_customer_reply(text) == text

    def test_a_lone_fence_is_removed_with_the_block_it_wrapped(self) -> None:
        text = 'Here you go:\n```\n{"name": "get_lead", "arguments": {}}\n```\nThanks!'
        assert clean_customer_reply(text) == "Here you go:\n\nThanks!"


class TestLabelledToolCallsAreRemoved:
    """The leak that reached a real customer: a bracketed label over its arguments.

    Observed in production, not hypothesised — a model emitted ``(book_demo)`` followed by
    the arguments, and removing only the arguments left the label standing as the entire
    customer-facing reply. Both halves have to go, and the guarantee is the whole reply
    reaching the approved fallback rather than a fragment.
    """

    def test_a_bracketed_label_over_its_arguments_is_removed(self) -> None:
        text = '(book_demo)\n{"start_time": "2026-09-30T06:30:00+00:00", "email": "a@b.com"}'
        assert clean_customer_reply(text) == ""

    @pytest.mark.parametrize(
        "label", ["(book_demo)", "[book_demo]", "book_demo", "book_demo:", '"book_demo"']
    )
    def test_a_lone_tool_label_is_never_sent(self, label: str) -> None:
        assert clean_customer_reply(label) == ""

    def test_a_label_above_prose_is_dropped_and_the_prose_kept(self) -> None:
        text = "(get_available_slots)\nLet me check what times are free."
        assert clean_customer_reply(text) == "Let me check what times are free."

    def test_a_label_between_two_sentences_leaves_both(self) -> None:
        # A single newline, not a blank line: `_drop_tool_label_lines` removes the line
        # itself and does not insert a paragraph break where it stood.
        text = "Let me check.\n(book_demo)\nYour demo is booked."
        assert clean_customer_reply(text) == "Let me check.\nYour demo is booked."


class TestEmailedInviteClaimsAreRemoved:
    """The falsehood that reached a real customer: after a successful booking the model
    said a calendar invite had been emailed to them. The platform has no email path, so
    the claim is unconditionally false — and the Sep 2026-09-28 staging message proves
    the prompt rule alone does not hold it. The guard is the deterministic layer."""

    def test_a_standalone_claim_is_dropped(self) -> None:
        assert clean_customer_reply("A calendar invite has been sent to test@test.com.") == ""

    def test_the_true_half_of_a_mixed_sentence_survives(self) -> None:
        # The staging shape: the Meet link is true, the emailed invite is not.
        text = (
            "Your SustainX presentation is booked for Tuesday 29 September, 9:00 AM IST. "
            "The Google Meet link is https://meet.google.com/fhj-pqvn-xkd and the calendar "
            "invite has been sent to test@test.com."
        )
        assert clean_customer_reply(text) == (
            "Your SustainX presentation is booked for Tuesday 29 September, 9:00 AM IST. "
            "The Google Meet link is https://meet.google.com/fhj-pqvn-xkd."
        )

    def test_a_claim_between_two_true_sentences_leaves_both(self) -> None:
        text = (
            "Your demo is booked for Tuesday 9:00 AM. "
            "I have emailed you the calendar invite. "
            "Let me know if you need anything else."
        )
        assert clean_customer_reply(text) == (
            "Your demo is booked for Tuesday 9:00 AM. Let me know if you need anything else."
        )

    @pytest.mark.parametrize(
        "claim",
        [
            "Please check your inbox for the calendar invite.",
            "You will receive the invitation by email shortly.",
            "I have sent the invite to your email address.",
        ],
    )
    def test_phrasing_variants_are_caught(self, claim: str) -> None:
        assert clean_customer_reply(f"Your demo is booked. {claim}") == "Your demo is booked."

    def test_a_negated_claim_is_left_alone(self) -> None:
        # "No invite will be emailed" is true. Removing it would punish honesty, and a
        # negation the patterns miss still passes — the prompts remain the first layer.
        text = "Your demo is booked. No invite will be emailed — confirmations come over WhatsApp."
        assert clean_customer_reply(text) == text

    def test_a_whatsapp_invite_without_an_email_channel_survives(self) -> None:
        # Bare "send you the invite" with no email channel may mean WhatsApp, which is
        # true. Only the emailed variants are matched, never this.
        text = "I will send you the invite here on WhatsApp."
        assert clean_customer_reply(text) == text

    def test_team_contact_by_email_is_not_an_invite_claim(self) -> None:
        text = "Our team will contact you by email shortly to arrange your offline demo."
        assert clean_customer_reply(text) == text
