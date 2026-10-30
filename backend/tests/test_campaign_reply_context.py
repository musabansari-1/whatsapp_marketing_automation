"""A reply to a campaign is answered in the context of the message it replies to."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.domain.ai_config import with_campaign_context
from app.worker.handlers.conversation import LastCampaign, _retrieval_query

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
        "I'm Interested\nlifeherba wellness pod studio"
    )


def test_real_questions_and_non_campaign_leads_search_as_asked() -> None:
    question = "What is the investment for the Studio and which cities are open?"
    assert _retrieval_query(question, POD) == question
    assert _retrieval_query("I'm Interested", None) == "I'm Interested"
