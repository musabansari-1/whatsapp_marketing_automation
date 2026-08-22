"""Conversation and lead state machines (specification §10, [D18], [D36]).

Pure logic: no database, no HTTP. §10 lists the states but not the edges, so the edges
are derived here and each is justified — review can challenge one edge rather than the
whole table.

**This module exists because §10 says the AI cannot invent state transitions.** In
Phase 10 the model will propose what it thinks happened; the backend decides what the
state becomes. Keeping the decision here, in code with no LLM anywhere near it, is what
makes that separation real rather than a line in a prompt.

Qualification is deliberately absent. [D9]/[D37] put the `QUALIFYING → QUALIFIED`
criteria in Phase 10, and inventing them early is exactly what the decision forbids.
The *transition* is permitted by the table below; what justifies taking it is not
decided here.
"""

from __future__ import annotations

from app.domain.enums import ConversationStatus, LeadStatus

# ---------------------------------------------------------------- conversations

# A CLOSED conversation is terminal ([D36]): a later message from the same customer
# starts a new conversation, which keeps each thread's history coherent for the AI.
_CONVERSATION_TRANSITIONS: dict[ConversationStatus, frozenset[ConversationStatus]] = {
    # The customer's turn has been handled; we are waiting on them, moving to booking,
    # or done.
    ConversationStatus.ACTIVE: frozenset(
        {
            ConversationStatus.WAITING_FOR_USER,
            ConversationStatus.BOOKING,
            ConversationStatus.CLOSED,
        }
    ),
    # A reply arrived, so it is our turn again. Booking can also start directly from
    # here: the customer's reply is often what accepts the demo.
    ConversationStatus.WAITING_FOR_USER: frozenset(
        {
            ConversationStatus.ACTIVE,
            ConversationStatus.BOOKING,
            ConversationStatus.CLOSED,
        }
    ),
    # Booking can complete back into normal conversation, wait on the customer for a
    # slot choice, or end.
    ConversationStatus.BOOKING: frozenset(
        {
            ConversationStatus.ACTIVE,
            ConversationStatus.WAITING_FOR_USER,
            ConversationStatus.CLOSED,
        }
    ),
    ConversationStatus.CLOSED: frozenset(),
}


# ---------------------------------------------------------------- leads

# Absent edges are absent on purpose. Two rules shape the table:
#
# - OPTED_OUT is terminal and reachable from everywhere. [D5]/[D16] make opt-out
#   final, and it can arrive at any moment in any state.
# - Progress is not forced to be linear. A customer can decline after booking, or
#   revive after saying no, because real conversations do that.
_LEAD_TRANSITIONS: dict[LeadStatus, frozenset[LeadStatus]] = {
    # A lead that has never been messaged. INVALID covers a number Meta rejects as
    # unreachable before any reply exists.
    LeadStatus.NEW: frozenset(
        {
            LeadStatus.CONTACTED,
            LeadStatus.RESPONDED,
            LeadStatus.INVALID,
            LeadStatus.FAILED,
        }
    ),
    # Messaged. Everything from here depends on whether they reply.
    LeadStatus.CONTACTED: frozenset(
        {
            LeadStatus.RESPONDED,
            LeadStatus.NOT_INTERESTED,
            LeadStatus.INVALID,
            LeadStatus.FAILED,
        }
    ),
    # They replied. Interest is the next judgement; a reply is not itself interest.
    LeadStatus.RESPONDED: frozenset(
        {
            LeadStatus.INTERESTED,
            LeadStatus.QUALIFYING,
            LeadStatus.NOT_INTERESTED,
        }
    ),
    LeadStatus.INTERESTED: frozenset(
        {
            LeadStatus.QUALIFYING,
            LeadStatus.DEMO_OFFERED,
            LeadStatus.NOT_INTERESTED,
        }
    ),
    # Information is being collected. What makes this end in QUALIFIED is a Phase 10
    # decision ([D37]) and is not encoded anywhere in this module.
    LeadStatus.QUALIFYING: frozenset(
        {
            LeadStatus.QUALIFIED,
            LeadStatus.INTERESTED,
            LeadStatus.NOT_INTERESTED,
        }
    ),
    LeadStatus.QUALIFIED: frozenset(
        {
            LeadStatus.DEMO_OFFERED,
            LeadStatus.DEMO_BOOKED,
            LeadStatus.NOT_INTERESTED,
        }
    ),
    # An offer can be accepted, declined, or leave the lead still being qualified if
    # the conversation goes back to questions.
    LeadStatus.DEMO_OFFERED: frozenset(
        {
            LeadStatus.DEMO_BOOKED,
            LeadStatus.QUALIFYING,
            LeadStatus.NOT_INTERESTED,
        }
    ),
    # A booked demo can be cancelled and re-offered; that is not a failure state.
    LeadStatus.DEMO_BOOKED: frozenset(
        {
            LeadStatus.DEMO_OFFERED,
            LeadStatus.NOT_INTERESTED,
        }
    ),
    # Not terminal: "not now" is not "never", and a later message can revive a lead.
    LeadStatus.NOT_INTERESTED: frozenset(
        {
            LeadStatus.RESPONDED,
            LeadStatus.INTERESTED,
            LeadStatus.QUALIFYING,
        }
    ),
    # Terminal. Consent withdrawn is not something the platform reverses on its own.
    LeadStatus.OPTED_OUT: frozenset(),
    # An unreachable number can be corrected by re-uploading a fixed one, which
    # produces a different lead; this one stays where it is.
    LeadStatus.INVALID: frozenset(),
    # A delivery failure is recoverable: the next campaign can try again.
    LeadStatus.FAILED: frozenset({LeadStatus.CONTACTED, LeadStatus.INVALID}),
}


class ConversationStateError(Exception):
    """The requested conversation transition is not allowed."""

    def __init__(self, current: ConversationStatus, target: ConversationStatus) -> None:
        super().__init__(f"Cannot move a conversation from {current.value} to {target.value}")
        self.current = current
        self.target = target


class LeadStateError(Exception):
    """The requested lead transition is not allowed."""

    def __init__(self, current: LeadStatus, target: LeadStatus) -> None:
        super().__init__(f"Cannot move a lead from {current.value} to {target.value}")
        self.current = current
        self.target = target


# ---------------------------------------------------------------- conversation API


def conversation_is_terminal(status: ConversationStatus) -> bool:
    return not _CONVERSATION_TRANSITIONS[status]


def allowed_conversation_transitions(
    status: ConversationStatus,
) -> frozenset[ConversationStatus]:
    return _CONVERSATION_TRANSITIONS[status]


def can_transition_conversation(current: ConversationStatus, target: ConversationStatus) -> bool:
    return target in _CONVERSATION_TRANSITIONS[current]


def assert_conversation_transition(current: ConversationStatus, target: ConversationStatus) -> None:
    """Raise unless the conversation transition is allowed.

    Same-state moves are rejected: a no-op transition usually means the caller lost
    track of the state rather than genuinely intending nothing.
    """
    if not can_transition_conversation(current, target):
        raise ConversationStateError(current, target)


# ---------------------------------------------------------------- lead API


def lead_is_terminal(status: LeadStatus) -> bool:
    return not _LEAD_TRANSITIONS[status]


def allowed_lead_transitions(status: LeadStatus) -> frozenset[LeadStatus]:
    """What a lead in this state may become.

    OPTED_OUT is added everywhere except where the lead is already terminal: consent
    can be withdrawn at any point in any conversation, so listing it on each row of
    the table would only invite one row to be forgotten.
    """
    allowed = _LEAD_TRANSITIONS[status]
    if not allowed:
        return allowed
    return allowed | {LeadStatus.OPTED_OUT}


def can_transition_lead(current: LeadStatus, target: LeadStatus) -> bool:
    return target in allowed_lead_transitions(current)


def assert_lead_transition(current: LeadStatus, target: LeadStatus) -> None:
    """Raise unless the lead transition is allowed."""
    if not can_transition_lead(current, target):
        raise LeadStateError(current, target)


__all__ = [
    "ConversationStateError",
    "LeadStateError",
    "allowed_conversation_transitions",
    "allowed_lead_transitions",
    "assert_conversation_transition",
    "assert_lead_transition",
    "can_transition_conversation",
    "can_transition_lead",
    "conversation_is_terminal",
    "lead_is_terminal",
]
