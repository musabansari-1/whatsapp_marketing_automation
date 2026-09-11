"""Campaign state machine and rule tests (specification §7, [D6], [D16]).

§7 lists the eight states but not the edges. The table is derived, so these tests
pin every edge that exists and — just as importantly — the ones that do not.
"""

from __future__ import annotations

import uuid

import pytest

from app.domain.campaign import (
    META_STATUS_APPROVED,
    CampaignNotSendableError,
    CampaignStateError,
    TemplateAssignmentError,
    TemplateFacts,
    TemplateNotApprovedError,
    TemplateOwnershipError,
    allowed_transitions,
    assert_sendable,
    assert_template_owned_by,
    assert_template_usable,
    assert_transition,
    can_transition,
    is_terminal,
    is_unsent,
    needs_assignment,
    recipient_status_for,
)
from app.domain.enums import CampaignStatus, RecipientStatus


class TestStates:
    def test_every_specified_state_is_in_the_table(self) -> None:
        """§7 lists exactly these eight."""
        assert set(CampaignStatus) == {
            CampaignStatus.DRAFT,
            CampaignStatus.READY,
            CampaignStatus.SCHEDULED,
            CampaignStatus.PROCESSING,
            CampaignStatus.PAUSED,
            CampaignStatus.COMPLETED,
            CampaignStatus.FAILED,
            CampaignStatus.CANCELLED,
        }
        for status in CampaignStatus:
            allowed_transitions(status)  # no state is missing from the table

    @pytest.mark.parametrize(
        "status",
        [CampaignStatus.COMPLETED, CampaignStatus.FAILED, CampaignStatus.CANCELLED],
    )
    def test_terminal_states_have_no_exit(self, status: CampaignStatus) -> None:
        """Re-running a finished campaign means creating a new one, so its metrics
        stay meaningful."""
        assert is_terminal(status)
        assert allowed_transitions(status) == frozenset()

    @pytest.mark.parametrize(
        "status",
        [
            CampaignStatus.DRAFT,
            CampaignStatus.READY,
            CampaignStatus.SCHEDULED,
            CampaignStatus.PROCESSING,
            CampaignStatus.PAUSED,
        ],
    )
    def test_non_terminal_states_have_an_exit(self, status: CampaignStatus) -> None:
        assert not is_terminal(status)


class TestAllowedTransitions:
    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (CampaignStatus.DRAFT, CampaignStatus.READY),
            (CampaignStatus.READY, CampaignStatus.PROCESSING),
            (CampaignStatus.READY, CampaignStatus.SCHEDULED),
            (CampaignStatus.SCHEDULED, CampaignStatus.PROCESSING),
            (CampaignStatus.PROCESSING, CampaignStatus.PAUSED),
            (CampaignStatus.PAUSED, CampaignStatus.PROCESSING),
            (CampaignStatus.PROCESSING, CampaignStatus.COMPLETED),
            (CampaignStatus.PROCESSING, CampaignStatus.FAILED),
        ],
    )
    def test_permitted(self, current: CampaignStatus, target: CampaignStatus) -> None:
        assert can_transition(current, target)
        assert_transition(current, target)

    @pytest.mark.parametrize(
        "current",
        [CampaignStatus.DRAFT, CampaignStatus.READY, CampaignStatus.PROCESSING],
    )
    def test_cancellation_is_available_while_running(self, current: CampaignStatus) -> None:
        assert can_transition(current, CampaignStatus.CANCELLED)


class TestForbiddenTransitions:
    def test_a_draft_cannot_be_sent_directly(self) -> None:
        """§7 requires preparation before the human confirmation."""
        assert not can_transition(CampaignStatus.DRAFT, CampaignStatus.PROCESSING)

    def test_a_completed_campaign_cannot_restart(self) -> None:
        assert not can_transition(CampaignStatus.COMPLETED, CampaignStatus.PROCESSING)

    def test_a_cancelled_campaign_cannot_be_revived(self) -> None:
        assert not can_transition(CampaignStatus.CANCELLED, CampaignStatus.READY)

    def test_a_failed_campaign_cannot_restart(self) -> None:
        assert not can_transition(CampaignStatus.FAILED, CampaignStatus.PROCESSING)

    def test_same_state_is_not_a_transition(self) -> None:
        """A no-op move usually means the caller lost track of the state."""
        assert not can_transition(CampaignStatus.PROCESSING, CampaignStatus.PROCESSING)

    def test_assert_transition_raises_with_both_states(self) -> None:
        with pytest.raises(CampaignStateError) as exc_info:
            assert_transition(CampaignStatus.COMPLETED, CampaignStatus.PROCESSING)
        assert exc_info.value.current is CampaignStatus.COMPLETED
        assert exc_info.value.target is CampaignStatus.PROCESSING


class TestTemplateApproval:
    def test_an_approved_marketing_template_is_usable(self) -> None:
        assert_template_usable(TemplateFacts(META_STATUS_APPROVED, is_marketing=True))

    @pytest.mark.parametrize("status", ["PENDING", "REJECTED", "PAUSED", "DISABLED", ""])
    def test_an_unapproved_template_is_refused(self, status: str) -> None:
        """[D6]: approval comes from Meta, and only APPROVED may be used."""
        with pytest.raises(TemplateNotApprovedError):
            assert_template_usable(TemplateFacts(status, is_marketing=True))

    def test_a_non_marketing_template_is_refused(self) -> None:
        """[D6]: a broadcast must use the approved template type for marketing.
        Sending a utility template as marketing is a policy violation Meta can
        penalise the number for."""
        with pytest.raises(TemplateNotApprovedError, match="MARKETING"):
            assert_template_usable(TemplateFacts(META_STATUS_APPROVED, is_marketing=False))

    def test_approval_is_never_inferred(self) -> None:
        """Only the literal value Meta reports counts; nothing is normalized into
        approval."""
        with pytest.raises(TemplateNotApprovedError):
            assert_template_usable(TemplateFacts("approved", is_marketing=True))


class TestTemplateOwnership:
    """[D57]: a campaign may use only a template its own product explicitly owns."""

    PRODUCT_A = uuid.UUID("0a000000-0000-4000-8000-00000000000a")
    PRODUCT_B = uuid.UUID("0b000000-0000-4000-8000-00000000000b")

    def test_a_template_owned_by_the_campaigns_product_is_usable(self) -> None:
        assert_template_owned_by(
            template_product_id=self.PRODUCT_A, campaign_product_id=self.PRODUCT_A
        )

    def test_an_unassigned_template_is_refused(self) -> None:
        """NULL is a shared-WABA artifact nobody has claimed, not a template every product
        may use. Reading it as "available to all" would let one product broadcast
        another's approved wording from its own number."""
        with pytest.raises(TemplateOwnershipError, match="not been assigned"):
            assert_template_owned_by(template_product_id=None, campaign_product_id=self.PRODUCT_A)

    def test_another_products_template_is_refused(self) -> None:
        with pytest.raises(TemplateOwnershipError, match="different product"):
            assert_template_owned_by(
                template_product_id=self.PRODUCT_B, campaign_product_id=self.PRODUCT_A
            )


class TestTemplateAssignment:
    PRODUCT_A = uuid.UUID("0a000000-0000-4000-8000-00000000000a")
    PRODUCT_B = uuid.UUID("0b000000-0000-4000-8000-00000000000b")

    def test_an_unassigned_template_can_be_assigned(self) -> None:
        assert needs_assignment(current_owner=None, target=self.PRODUCT_A) is True

    def test_assigning_to_the_current_owner_changes_nothing(self) -> None:
        """A repeated request is harmless rather than an error."""
        assert needs_assignment(current_owner=self.PRODUCT_A, target=self.PRODUCT_A) is False

    def test_an_owned_template_cannot_move_to_another_product(self) -> None:
        """One owner per provider template. Moving it would silently change what the
        first product's existing campaigns may send."""
        with pytest.raises(TemplateAssignmentError, match="another product"):
            needs_assignment(current_owner=self.PRODUCT_A, target=self.PRODUCT_B)


class TestSendability:
    def test_a_ready_campaign_with_recipients_is_sendable(self) -> None:
        assert_sendable(CampaignStatus.READY, 5)

    @pytest.mark.parametrize(
        "status",
        [
            CampaignStatus.DRAFT,
            CampaignStatus.PROCESSING,
            CampaignStatus.COMPLETED,
            CampaignStatus.CANCELLED,
            CampaignStatus.PAUSED,
        ],
    )
    def test_only_a_ready_campaign_may_be_sent(self, status: CampaignStatus) -> None:
        with pytest.raises(CampaignNotSendableError):
            assert_sendable(status, 5)

    def test_a_campaign_with_no_recipients_is_refused(self) -> None:
        """Otherwise the broadcast reports PROCESSING then silently completes having
        done nothing, which reads as a delivery failure rather than an empty
        audience."""
        with pytest.raises(CampaignNotSendableError, match="no recipients"):
            assert_sendable(CampaignStatus.READY, 0)


class TestSuppression:
    def test_a_suppressed_number_is_skipped_not_failed(self) -> None:
        """[D16]: respecting an opt-out is not a delivery failure, and campaign
        reporting must not describe it as one."""
        assert recipient_status_for(suppressed=True) is RecipientStatus.SKIPPED
        assert recipient_status_for(suppressed=False) is RecipientStatus.PENDING


class TestUnsentRecipients:
    @pytest.mark.parametrize("status", [RecipientStatus.PENDING, RecipientStatus.QUEUED])
    def test_outstanding_work(self, status: RecipientStatus) -> None:
        assert is_unsent(status)

    @pytest.mark.parametrize(
        "status",
        [
            RecipientStatus.SENT,
            RecipientStatus.DELIVERED,
            RecipientStatus.READ,
            RecipientStatus.FAILED,
            RecipientStatus.SKIPPED,
        ],
    )
    def test_settled_recipients_are_not_resent(self, status: RecipientStatus) -> None:
        """The basis of send idempotency: nobody already handled is enqueued again."""
        assert not is_unsent(status)
