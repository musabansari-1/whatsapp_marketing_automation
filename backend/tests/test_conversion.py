"""Product conversion mode helpers."""

from __future__ import annotations

import pytest

from app.ai import tools as ai_tools
from app.domain.conversion import (
    ConversionMode,
    parse_conversion_mode,
    uses_calendar_booking,
    with_conversion_guidance,
)
from app.integrations.llm import AuthorizedTool


class TestConversionMode:
    def test_default_is_calendar_booking(self) -> None:
        assert parse_conversion_mode(None) is ConversionMode.CALENDAR_BOOKING
        assert uses_calendar_booking(None)

    def test_external_registration_is_recognised(self) -> None:
        assert parse_conversion_mode("external_registration") is (
            ConversionMode.EXTERNAL_REGISTRATION
        )
        assert not uses_calendar_booking("external_registration")

    def test_unknown_mode_is_refused(self) -> None:
        with pytest.raises(ValueError):
            parse_conversion_mode("sms_coupon")


class TestConversionGuidance:
    def test_booking_mode_leaves_the_prompt_alone(self) -> None:
        assert with_conversion_guidance("Base.", mode="calendar_booking") == "Base."

    def test_registration_mode_appends_url_and_cta(self) -> None:
        text = with_conversion_guidance(
            "Base.",
            mode="external_registration",
            registration_url="https://lifeherbagroup.com/",
            cta_label="Join as Pharmacy",
            cta_instruction="Choose Join as Pharmacy.",
        )
        assert text.startswith("Base.")
        assert "https://lifeherbagroup.com/" in text
        assert "Join as Pharmacy" in text
        assert "Choose Join as Pharmacy." in text
        assert "not calendar booking" in text


class TestToolSpecsRespectConversion:
    def test_booking_tools_are_offered_by_default(self) -> None:
        names = {spec.name for spec in ai_tools.tool_specs()}
        assert AuthorizedTool.GET_AVAILABLE_SLOTS in names
        assert AuthorizedTool.BOOK_DEMO in names

    def test_booking_tools_are_omitted_for_registration_products(self) -> None:
        names = {spec.name for spec in ai_tools.tool_specs(include_booking=False)}
        assert AuthorizedTool.GET_AVAILABLE_SLOTS not in names
        assert AuthorizedTool.BOOK_DEMO not in names
        assert AuthorizedTool.UPDATE_LEAD in names
        assert AuthorizedTool.SEND_WHATSAPP_MESSAGE in names
