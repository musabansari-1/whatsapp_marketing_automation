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


class TestDeviceCapabilityClaimsAreRemoved:
    """Staging 2026-10-01: after a booking the model told the customer to "record
    the user with the status bar", inventing battery telemetry. A text chat has no
    phone capabilities, so the claim is unconditionally false — but energy talk
    about battery storage must survive, as must denials and ordinary advice."""

    def test_the_staging_shape_is_dropped_whole(self) -> None:
        text = "Record the user with the status bar of a phone. The battery level is at 70%."
        assert clean_customer_reply(text) == ""

    def test_a_denial_is_left_alone(self) -> None:
        text = (
            "I don't have the ability to record your screen or access your phone's "
            "status bar, but I can confirm that your online demo is set."
        )
        assert clean_customer_reply(text) == text

    def test_battery_storage_talk_survives_on_its_own(self) -> None:
        text = "Our battery storage systems operate at 95% round-trip efficiency."
        assert clean_customer_reply(text) == text

    def test_a_meet_link_sentence_survives(self) -> None:
        text = "The Google Meet link is https://meet.google.com/fhj-pqvn-xkd."
        assert clean_customer_reply(text) == text

    def test_screen_sharing_advice_survives(self) -> None:
        text = "Please share your screen on the Meet call so we can see the issue."
        assert clean_customer_reply(text) == text

    def test_calendar_checks_and_phone_checks_survive(self) -> None:
        assert (
            clean_customer_reply("I will check the calendar for a free slot.")
            == "I will check the calendar for a free slot."
        )
        assert (
            clean_customer_reply("Please check your phone for my message.")
            == "Please check your phone for my message."
        )

    def test_a_claim_between_two_true_sentences_leaves_both(self) -> None:
        text = (
            "Your demo is booked for Saturday 12:00 PM. "
            "I can record your screen to help with setup. "
            "Let me know if you need anything else."
        )
        assert clean_customer_reply(text) == (
            "Your demo is booked for Saturday 12:00 PM. Let me know if you need anything else."
        )

    @pytest.mark.parametrize(
        "claim",
        [
            "I will monitor the status bar during the call.",
            "I can access your camera for verification.",
        ],
    )
    def test_claim_variants_are_caught(self, claim: str) -> None:
        assert clean_customer_reply(f"Your demo is booked. {claim}") == "Your demo is booked."


class TestThinkingAloudIsBlanked:
    """Staging 2026-10-01: a Life Herba reply was pure chain-of-thought — "the
    user is asking… looking at my instructions… let me make the function call
    properly" — with zero tool calls behind it. Thinking is never addressed to
    anyone, and the plan fragments it leaves behind read worse than the approved
    fallback, so any match blanks the whole reply instead of salvaging lines."""

    STAGING_TEXT = (
        'The user is asking about "therapy details of LifeHerba." This could be '
        "interpreted in different ways - they might be asking about therapy services. "
        "Looking at my instructions, I need to answer first and be careful with "
        "the tool call format. Let me make the function call properly."
    )

    def test_the_staging_shape_is_blanked_whole(self) -> None:
        assert clean_customer_reply(self.STAGING_TEXT) == ""

    @pytest.mark.parametrize(
        "thinking",
        [
            "The customer is asking about pricing, let me check.",
            "Looking at my instructions, I should offer a demo.",
            "I need to be careful with the tool call format.",
            "Let me think about the best slot.",
        ],
    )
    def test_thinking_variants_blank_the_reply(self, thinking: str) -> None:
        assert clean_customer_reply(thinking) == ""

    @pytest.mark.parametrize(
        "prose",
        [
            "Would you prefer a video call or an in-person demo?",
            "Please call us on +911234567890 for support.",
            "Here are the tools we offer for simulation-based learning.",
            "I will check the calendar for a free slot.",
        ],
    )
    def test_ordinary_prose_survives(self, prose: str) -> None:
        assert clean_customer_reply(prose) == prose


class TestWhatsAppFormattingIsNormalised:
    """Staging 2026-10-10: a reply arrived as `*Wellness supplements: *SUKOON
    HERB …` — an opened `*` the model never closed. The model writes formatting
    one token at a time with no syntax check, so pairing is enforced here:
    emphasis survives, stray syntax never reaches the customer."""

    def test_markdown_bold_becomes_whatsapp_bold(self) -> None:
        assert clean_customer_reply("Try our **new plan** today.") == "Try our *new plan* today."

    def test_paired_whatsapp_markers_survive(self) -> None:
        text = "See *Practitioner consultations* and _teleconsultation_ options."
        assert clean_customer_reply(text) == text

    def test_the_staging_stray_stars_are_removed(self) -> None:
        text = "*Wellness supplements: *SUKOON HERB and SUKOON PLUS are listed here."
        assert (
            clean_customer_reply(text)
            == "Wellness supplements: SUKOON HERB and SUKOON PLUS are listed here."
        )

    def test_a_lone_closer_is_removed(self) -> None:
        assert clean_customer_reply("Welcome to Life Herba!*") == "Welcome to Life Herba!"

    def test_math_and_identifiers_are_literals(self) -> None:
        assert clean_customer_reply("Take 2 * 3 tablets daily.") == "Take 2 * 3 tablets daily."
        assert clean_customer_reply("Your record_id is 42.") == "Your record_id is 42."
        assert clean_customer_reply("A*B testing is underway.") == "A*B testing is underway."

    def test_markers_inside_urls_survive(self) -> None:
        text = "Browse https://lifeherbagroup.com/app/a*b_c for details."
        assert clean_customer_reply(text) == text

    def test_star_bullets_become_dots(self) -> None:
        assert clean_customer_reply("* Yoga\n* Supplements") == "• Yoga\n• Supplements"

    def test_unpaired_underscore_is_removed_but_italic_survives(self) -> None:
        assert (
            clean_customer_reply("_Teleconsultation_ is available.")
            == "_Teleconsultation_ is available."
        )
        assert clean_customer_reply("Call us on _this number.") == "Call us on this number."

    def test_colon_closing_star_keeps_its_pair(self) -> None:
        """Staging 2026-10-10: `**Registration URL …:**` came out as
        `Registration URL …:*` — the punctuation-adjacent closer was not
        classified as a closer, so its partner opener was dropped and the
        stray star reached the customer. The pair must survive whole."""
        text = "**Registration URL (share exactly as configured):**"
        assert clean_customer_reply(text) == "*Registration URL (share exactly as configured):*"

    def test_trailing_star_after_punctuation_is_removed(self) -> None:
        assert clean_customer_reply("What to do next:*") == "What to do next:"

    def test_stars_around_a_quoted_phrase_survive(self) -> None:
        text = 'Select *"Consumer / General User"* inside the app.'
        assert clean_customer_reply(text) == text

    def test_heading_markers_are_stripped(self) -> None:
        """WhatsApp renders `###` literally, and the model echoes heading markers
        straight out of retrieved Markdown knowledge however often the prompt
        forbids them."""
        text = "### 🌿 LifeHerba\nDigestion support from herbal formulations."
        assert (
            clean_customer_reply(text)
            == "🌿 LifeHerba\nDigestion support from herbal formulations."
        )
        assert clean_customer_reply("## How it works") == "How it works"

    def test_a_hashes_only_line_vanishes(self) -> None:
        assert clean_customer_reply("###\nDownload the app to register.") == (
            "Download the app to register."
        )

    def test_heading_markers_run_of_one_to_six(self) -> None:
        assert clean_customer_reply("# One") == "One"
        assert clean_customer_reply("###### Six") == "Six"

    def test_heading_containing_bold_keeps_the_bold(self) -> None:
        assert clean_customer_reply("## **Benefits**") == "*Benefits*"

    def test_hashtags_and_hash_characters_are_literals(self) -> None:
        """Only a line-start `#` followed by whitespace is a heading: `#tag`,
        `C#` and `#1` are ordinary text."""
        assert clean_customer_reply("#immunity and #metabolism") == "#immunity and #metabolism"
        assert clean_customer_reply("C# is a language") == "C# is a language"
        assert clean_customer_reply("#1 choice for wellness") == "#1 choice for wellness"
