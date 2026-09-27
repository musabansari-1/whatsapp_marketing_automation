"""Shipped booking prompts must not promise follow-up the platform cannot perform.

D-22 (2026-09-22): when `get_available_slots` / `book_demo` failed, the agent told a
customer it would "send slots via email" and "verify when the system is back". It can
do neither. The rule lives in the product prompts (Academicae, SustainX — the two
calendar-booking products) so the behaviour survives any prompt rewrite, and the tool
errors in `app.ai.tools` carry the same guidance for the moment the failure happens.
"""

from __future__ import annotations

from pathlib import Path

from app.configuration.catalog import load_catalog

SHIPPED = Path(__file__).resolve().parents[1] / "config" / "products.json"

# Exactly what the model must not promise, phrased as the prompts state it.
FORBIDDEN_PROMISES = (
    "do not promise to check again later",
    "do not promise to email times or a calendar invite",
    "do not promise to confirm the booking later",
    "you cannot do any of these",
)


def _product(slug: str):
    return {p.slug: p for p in load_catalog(SHIPPED).products}[slug]


class TestBookingFailureGuidance:
    def test_both_booking_products_forbid_promises_they_cannot_keep(self) -> None:
        for slug, team in (("academicae", "Academicae"), ("sustainx", "SustainX")):
            product = _product(slug)
            assert product.ai_configuration is not None
            prompt = product.ai_configuration.system_prompt or ""
            for phrase in FORBIDDEN_PROMISES:
                assert phrase in prompt, f"{slug}: missing {phrase!r}"
            assert f"colleague from the {team} team will follow up" in prompt, slug

    def test_the_guidance_says_what_to_do_instead(self) -> None:
        """Not just a prohibition: the model needs the sentence it may send."""
        for slug in ("academicae", "sustainx"):
            prompt = _product(slug).ai_configuration.system_prompt or ""
            assert "the calendar cannot be checked right now" in prompt, slug
            assert prompt.count("will follow up") >= 2, slug  # flow + booking rules

    def test_both_booking_products_never_invent_a_meet_link(self) -> None:
        """The join link only exists when the calendar could host a conference; a
        secondary calendar in a personal-Google account has none. The model must
        not promise or produce one from an empty `meeting_link`."""
        for slug in ("academicae", "sustainx"):
            prompt = _product(slug).ai_configuration.system_prompt or ""
            assert "Only mention a Meet join link" in prompt, slug
            assert "Never invent or guess a link" in prompt, slug

    def test_both_booking_products_never_claim_an_emailed_invite(self) -> None:
        """Staging 2026-09-27: after a successful SustainX booking the model told
        the customer "the calendar invite has been sent" to their email. Nothing
        sends email — the customer is not added to the event (service-account
        auth cannot invite) and `sendUpdates` stays `none` — so the success path
        needs the same honesty as the failure path."""
        for slug in ("academicae", "sustainx"):
            prompt = _product(slug).ai_configuration.system_prompt or ""
            assert "Never tell the customer a calendar invite or confirmation was emailed" in prompt, slug
            assert "confirmations go over WhatsApp" in prompt, slug

    def test_life_herba_needs_no_calendar_guidance(self) -> None:
        """It converts by external registration: booking tools are not offered, so the
        rule belongs nowhere near its prompts."""
        product = _product("life-herba")
        prompt = product.ai_configuration.system_prompt or ""
        assert "do not promise to email times" not in prompt
