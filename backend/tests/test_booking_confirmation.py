"""Unit tests for the booking bell (dashboard copy) and the event description.

The customer confirmation used to live here too: a second WhatsApp message sent
by the notify task. It double-texted every booking — the turn's reply already
confirms the time and join link — so the task is team-only now and the copy,
setting and send path are gone with it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from app.ai.tools import _booking_description
from app.worker.handlers.booking import _notification_message


def _booking(*, meeting_link: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        start_time=datetime(2026, 9, 2, 4, 30, tzinfo=UTC),
        timezone="Asia/Kolkata",
        meeting_link=meeting_link,
    )


def test_dashboard_notification_includes_meet_link() -> None:
    booking = _booking(meeting_link="https://meet.google.com/abc-defg-hij")
    lead = SimpleNamespace(full_name="Asha Rao", phone_e164="+919876543210", company="Northwind")

    message = _notification_message(booking, lead)  # type: ignore[arg-type]

    assert "Asha Rao" in message
    assert "https://meet.google.com/abc-defg-hij" in message


def _lead(**overrides: object) -> SimpleNamespace:
    fields: dict[str, object] = {
        "full_name": "Asha Rao",
        "phone_e164": "+919876543210",
        "company": "Northwind",
        "designation": None,
        "city": None,
        "email": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class TestBookingDescriptionContactRouting:
    """Where the team reaches the customer when Google cannot.

    A service account without Domain-Wide Delegation cannot invite anyone, so
    the customer's email is the contact channel instead: it goes into the event
    description, with the instruction that matters — share the Meet link on
    WhatsApp, because no calendar email will ever arrive to do it.

    These pin `_booking_description`, not the provider. They fail together if
    the email stops reaching the description, which is how a regression here
    would silently re-create the gap the provider fix closes.
    """

    def test_a_lead_without_email_describes_only_the_conversation(self) -> None:
        body = _booking_description(_lead())

        assert "Demo booked from a WhatsApp conversation" in body
        assert "+919876543210" in body
        assert "Company: Northwind" in body
        assert "Email:" not in body

    def test_a_lead_with_email_carries_the_contact_channel_and_instruction(
        self,
    ) -> None:
        body = _booking_description(_lead(email="asha@example.com"))

        assert "Email: asha@example.com" in body
        assert "share the Google Meet link on WhatsApp" in body

    def test_extra_facts_do_not_push_the_email_or_instruction_out(self) -> None:
        body = _booking_description(_lead(email="asha@example.com", city="Pune"))

        assert "City: Pune" in body
        assert "Email: asha@example.com" in body
