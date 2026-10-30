"""A reply to a campaign is answered in the context of the message it replies to."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime

from app.domain.ai_config import with_campaign_context, with_lead_capture_guidance
from app.worker.handlers.conversation import (
    LastCampaign,
    _campaign_asks_to_register,
    _retrieval_query,
)

POD = LastCampaign(
    template_name="lifeherba_wellness_pod_studio",
    template_body="Hello {{1}},\n\nIntroducing LifeHerba Wellness Pod",
    segment_id=uuid.uuid4(),
    sent_at=datetime(2026, 10, 30, 9, 54, tzinfo=UTC),
)


def test_campaign_block_carries_template_and_body() -> None:
    text = with_campaign_context(
        "Base prompt.", template_name=POD.template_name, body=POD.template_body
    )
    assert text.startswith("Base prompt.\n\n## Last campaign message this customer received")
    assert "Template: lifeherba_wellness_pod_studio" in text
    assert "Introducing LifeHerba Wellness Pod" in text


def test_no_body_leaves_prompt_unchanged() -> None:
    name = POD.template_name
    assert with_campaign_context("Base prompt.", template_name=name, body=None) == "Base prompt."
    assert with_campaign_context("Base prompt.", template_name=name, body="  ") == "Base prompt."


def test_button_tap_is_searched_with_the_campaign_topic() -> None:
    assert _retrieval_query("I'm Interested", POD) == (
        "Reply to the \"I'm Interested\" button\nI'm Interested\nlifeherba wellness pod studio"
    )


def test_real_questions_and_non_campaign_leads_search_as_asked() -> None:
    question = "What is the investment for the Studio and which cities are open?"
    assert _retrieval_query(question, POD) == question
    assert _retrieval_query("I'm Interested", None) == "I'm Interested"


def test_only_a_campaign_that_invites_registration_keeps_the_registration_goal() -> None:
    onboarding = dataclasses.replace(
        POD, template_body="Hello {{1}}, tap *Register Now* to join LifeHerba."
    )
    assert _campaign_asks_to_register(onboarding)
    assert not _campaign_asks_to_register(POD)


def test_lead_capture_guidance_rules_out_the_app() -> None:
    text = with_lead_capture_guidance("Base prompt.")
    assert text.startswith("Base prompt.\n\n## Goal of this conversation")
    assert "Do not ask the customer to download the app" in text
