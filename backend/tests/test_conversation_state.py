"""Conversation and lead state machine tests (specification §10, [D36]).

The transition table is the mechanism that stops the AI inventing states, so the tests
assert the edges that matter rather than restating the table.
"""

from __future__ import annotations

import pytest

from app.domain.conversation import (
    ConversationStateError,
    LeadStateError,
    allowed_conversation_transitions,
    allowed_lead_transitions,
    assert_conversation_transition,
    assert_lead_transition,
    can_transition_conversation,
    can_transition_lead,
    conversation_is_terminal,
    lead_is_terminal,
)
from app.domain.enums import ConversationStatus, LeadStatus


class TestConversationTransitions:
    def test_every_state_is_in_the_table(self) -> None:
        """A state missing from the table would raise KeyError the first time a
        conversation reached it."""
        for status in ConversationStatus:
            assert isinstance(allowed_conversation_transitions(status), frozenset)

    def test_replying_moves_to_waiting(self) -> None:
        assert can_transition_conversation(
            ConversationStatus.ACTIVE, ConversationStatus.WAITING_FOR_USER
        )

    def test_a_customer_reply_makes_it_our_turn_again(self) -> None:
        assert can_transition_conversation(
            ConversationStatus.WAITING_FOR_USER, ConversationStatus.ACTIVE
        )

    def test_booking_is_reachable_from_both_talking_states(self) -> None:
        """The reply that accepts a demo can arrive in either state."""
        assert can_transition_conversation(ConversationStatus.ACTIVE, ConversationStatus.BOOKING)
        assert can_transition_conversation(
            ConversationStatus.WAITING_FOR_USER, ConversationStatus.BOOKING
        )

    def test_closed_is_terminal(self) -> None:
        """[D36]: a closed conversation is never reopened. A later message starts a
        new one, which keeps each thread's history coherent."""
        assert conversation_is_terminal(ConversationStatus.CLOSED)
        assert allowed_conversation_transitions(ConversationStatus.CLOSED) == frozenset()

    @pytest.mark.parametrize(
        "target",
        [
            ConversationStatus.ACTIVE,
            ConversationStatus.WAITING_FOR_USER,
            ConversationStatus.BOOKING,
        ],
    )
    def test_nothing_reopens_a_closed_conversation(self, target: ConversationStatus) -> None:
        assert not can_transition_conversation(ConversationStatus.CLOSED, target)

        with pytest.raises(ConversationStateError):
            assert_conversation_transition(ConversationStatus.CLOSED, target)

    def test_every_state_can_be_closed(self) -> None:
        """A conversation must always be endable; otherwise a stuck thread has no way
        out."""
        for status in ConversationStatus:
            if status is ConversationStatus.CLOSED:
                continue
            assert can_transition_conversation(status, ConversationStatus.CLOSED)

    def test_a_no_op_transition_is_refused(self) -> None:
        """Usually means the caller lost track of the state rather than genuinely
        intending nothing."""
        with pytest.raises(ConversationStateError):
            assert_conversation_transition(ConversationStatus.ACTIVE, ConversationStatus.ACTIVE)

    def test_the_error_names_both_states(self) -> None:
        with pytest.raises(ConversationStateError) as caught:
            assert_conversation_transition(ConversationStatus.CLOSED, ConversationStatus.ACTIVE)

        assert caught.value.current is ConversationStatus.CLOSED
        assert caught.value.target is ConversationStatus.ACTIVE


class TestLeadTransitions:
    def test_every_state_is_in_the_table(self) -> None:
        for status in LeadStatus:
            assert isinstance(allowed_lead_transitions(status), frozenset)

    def test_the_normal_path_to_a_booked_demo(self) -> None:
        path = [
            LeadStatus.NEW,
            LeadStatus.CONTACTED,
            LeadStatus.RESPONDED,
            LeadStatus.INTERESTED,
            LeadStatus.QUALIFYING,
            LeadStatus.QUALIFIED,
            LeadStatus.DEMO_OFFERED,
            LeadStatus.DEMO_BOOKED,
        ]
        for current, target in zip(path, path[1:], strict=False):
            assert can_transition_lead(current, target), f"{current} -> {target}"

    def test_opting_out_is_possible_from_every_live_state(self) -> None:
        """[D5]/[D16]: consent can be withdrawn at any moment, in any state."""
        for status in LeadStatus:
            if lead_is_terminal(status):
                continue
            assert can_transition_lead(status, LeadStatus.OPTED_OUT), status

    def test_opted_out_is_terminal(self) -> None:
        """The platform does not reverse a withdrawal of consent on its own."""
        assert lead_is_terminal(LeadStatus.OPTED_OUT)

        with pytest.raises(LeadStateError):
            assert_lead_transition(LeadStatus.OPTED_OUT, LeadStatus.RESPONDED)

    def test_a_lead_cannot_skip_straight_to_qualified(self) -> None:
        """Qualification follows information collection. This is the edge that stops a
        confident model declaring a lead qualified out of nowhere."""
        assert not can_transition_lead(LeadStatus.NEW, LeadStatus.QUALIFIED)
        assert not can_transition_lead(LeadStatus.CONTACTED, LeadStatus.QUALIFIED)
        assert not can_transition_lead(LeadStatus.RESPONDED, LeadStatus.QUALIFIED)

    def test_a_lead_cannot_go_backwards(self) -> None:
        assert not can_transition_lead(LeadStatus.QUALIFIED, LeadStatus.NEW)
        assert not can_transition_lead(LeadStatus.RESPONDED, LeadStatus.CONTACTED)

    def test_not_interested_can_be_revived(self) -> None:
        """ "Not now" is not "never" — a later message can bring a lead back."""
        assert not lead_is_terminal(LeadStatus.NOT_INTERESTED)
        assert can_transition_lead(LeadStatus.NOT_INTERESTED, LeadStatus.INTERESTED)

    def test_a_booked_demo_can_be_rescheduled_or_declined(self) -> None:
        assert can_transition_lead(LeadStatus.DEMO_BOOKED, LeadStatus.DEMO_OFFERED)
        assert can_transition_lead(LeadStatus.DEMO_BOOKED, LeadStatus.NOT_INTERESTED)

    def test_a_failed_delivery_can_be_retried_by_a_later_campaign(self) -> None:
        assert can_transition_lead(LeadStatus.FAILED, LeadStatus.CONTACTED)

    def test_an_invalid_number_stays_invalid(self) -> None:
        """Correcting it produces a different lead, since the phone is the identity
        ([D11])."""
        assert lead_is_terminal(LeadStatus.INVALID)

    def test_a_no_op_transition_is_refused(self) -> None:
        with pytest.raises(LeadStateError):
            assert_lead_transition(LeadStatus.RESPONDED, LeadStatus.RESPONDED)

    def test_the_error_names_both_states(self) -> None:
        with pytest.raises(LeadStateError) as caught:
            assert_lead_transition(LeadStatus.NEW, LeadStatus.QUALIFIED)

        assert caught.value.current is LeadStatus.NEW
        assert caught.value.target is LeadStatus.QUALIFIED
