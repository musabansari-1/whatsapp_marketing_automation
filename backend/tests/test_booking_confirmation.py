"""Unit tests for booking confirmation copy (Meet link on WhatsApp)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from app.core.config import Settings
from app.worker.handlers.booking import _confirmation_message, _notification_message


def _booking(*, meeting_link: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        start_time=datetime(2026, 9, 2, 4, 30, tzinfo=UTC),
        timezone="Asia/Kolkata",
        meeting_link=meeting_link,
    )


def test_confirmation_includes_meet_link() -> None:
    settings = Settings(
        msg_booking_confirmation=(
            "Your demo is confirmed for {start_time} ({timezone}). Join here: {meeting_link}"
        )
    )
    booking = _booking(meeting_link="https://meet.google.com/abc-defg-hij")

    body = _confirmation_message(settings, booking)  # type: ignore[arg-type]

    assert "10:00 AM" in body
    assert "https://meet.google.com/abc-defg-hij" in body


def test_confirmation_falls_back_without_meet_link() -> None:
    settings = Settings(
        msg_booking_confirmation=(
            "Your demo is confirmed for {start_time} ({timezone}). Join here: {meeting_link}"
        )
    )
    booking = _booking(meeting_link=None)

    body = _confirmation_message(settings, booking)  # type: ignore[arg-type]

    assert "10:00 AM" in body
    assert "Join here" not in body


def test_dashboard_notification_includes_meet_link() -> None:
    booking = _booking(meeting_link="https://meet.google.com/abc-defg-hij")
    lead = SimpleNamespace(full_name="Asha Rao", phone_e164="+919876543210", company="Northwind")

    message = _notification_message(booking, lead)  # type: ignore[arg-type]

    assert "Asha Rao" in message
    assert "https://meet.google.com/abc-defg-hij" in message
