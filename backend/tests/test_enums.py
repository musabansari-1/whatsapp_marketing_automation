"""State enum tests.

The specification enumerates several of these exactly. Transcription errors would be
invisible at runtime until a state failed to round-trip, so the spec'd sets are
asserted literally rather than by spot check.
"""

from __future__ import annotations

from app.domain.enums import (
    BookingStatus,
    CampaignStatus,
    ConversationStatus,
    LeadStatus,
    MessageStatus,
    RecipientStatus,
    UserRole,
)


class TestSpecifiedEnums:
    def test_campaign_states(self) -> None:
        """Specification §7."""
        assert [s.value for s in CampaignStatus] == [
            "DRAFT",
            "READY",
            "SCHEDULED",
            "PROCESSING",
            "PAUSED",
            "COMPLETED",
            "FAILED",
            "CANCELLED",
        ]

    def test_lead_states(self) -> None:
        """Specification §10."""
        assert [s.value for s in LeadStatus] == [
            "NEW",
            "CONTACTED",
            "RESPONDED",
            "INTERESTED",
            "QUALIFYING",
            "QUALIFIED",
            "DEMO_OFFERED",
            "DEMO_BOOKED",
            "NOT_INTERESTED",
            "OPTED_OUT",
            "INVALID",
            "FAILED",
        ]

    def test_conversation_states(self) -> None:
        """Specification §10."""
        assert [s.value for s in ConversationStatus] == [
            "ACTIVE",
            "WAITING_FOR_USER",
            "BOOKING",
            "CLOSED",
        ]

    def test_message_states(self) -> None:
        """Specification §8."""
        assert [s.value for s in MessageStatus] == [
            "QUEUED",
            "SENT",
            "DELIVERED",
            "READ",
            "FAILED",
        ]

    def test_booking_states(self) -> None:
        """Specification §15."""
        assert [s.value for s in BookingStatus] == [
            "PENDING",
            "CONFIRMED",
            "CANCELLED",
            "FAILED",
        ]

    def test_user_roles(self) -> None:
        """[D2]. Exactly two roles; no public registration implies no other."""
        assert [r.value for r in UserRole] == ["ADMIN", "SALESPERSON"]


class TestDerivedEnums:
    def test_recipient_status_includes_skipped(self) -> None:
        """[D16]: a suppressed recipient is skipped, and skipped is not a failure.

        Distinguishing the two matters for reporting: a campaign that skipped
        opted-out numbers has not partially failed.
        """
        values = {s.value for s in RecipientStatus}
        assert "SKIPPED" in values
        assert "FAILED" in values
        # Distinct members, so campaign counters can report them separately.
        assert len(values) == len(RecipientStatus)


class TestSingleSourceOfTruth:
    def test_whatsapp_provider_reuses_the_domain_enums(self) -> None:
        """Persistence and the integration must agree on one set of states."""
        from app.domain.enums import TemplateCategory
        from app.integrations.whatsapp import MessageStatus as ProviderMessageStatus
        from app.integrations.whatsapp import TemplateCategory as ProviderTemplateCategory

        assert ProviderMessageStatus is MessageStatus
        assert ProviderTemplateCategory is TemplateCategory
