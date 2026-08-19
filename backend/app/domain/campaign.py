"""Campaign rules and state machine (specification §7, [D6], [D16]).

Pure logic: no database, no HTTP. The transition table is the authority on what a
campaign may do next, so the API cannot move a campaign somewhere the specification
does not allow just because a handler forgot to check.

§7 lists the eight states but not the edges between them. Those are derived here and
each is justified, so review can challenge any single edge rather than the whole
table.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.enums import CampaignStatus, RecipientStatus

# Meta's approval value. [D6] forbids inventing a local approval status, so this is
# the value Meta reports, compared as-is.
META_STATUS_APPROVED = "APPROVED"

# Allowed transitions. Absent edges are absent on purpose.
_TRANSITIONS: dict[CampaignStatus, frozenset[CampaignStatus]] = {
    # A draft becomes ready once it has a usable snapshot, or is abandoned.
    CampaignStatus.DRAFT: frozenset(
        {CampaignStatus.READY, CampaignStatus.CANCELLED, CampaignStatus.FAILED}
    ),
    # Ready is the state a human confirms from (§7). Scheduling is a state the
    # specification lists; nothing in V1 enters it yet.
    CampaignStatus.READY: frozenset(
        {CampaignStatus.PROCESSING, CampaignStatus.SCHEDULED, CampaignStatus.CANCELLED}
    ),
    CampaignStatus.SCHEDULED: frozenset(
        {CampaignStatus.PROCESSING, CampaignStatus.READY, CampaignStatus.CANCELLED}
    ),
    # Sending can be paused, finish, or fail outright.
    CampaignStatus.PROCESSING: frozenset(
        {
            CampaignStatus.PAUSED,
            CampaignStatus.COMPLETED,
            CampaignStatus.FAILED,
            CampaignStatus.CANCELLED,
        }
    ),
    CampaignStatus.PAUSED: frozenset({CampaignStatus.PROCESSING, CampaignStatus.CANCELLED}),
    # Terminal. A finished campaign is history; re-running means a new campaign, so
    # its metrics stay meaningful.
    CampaignStatus.COMPLETED: frozenset(),
    CampaignStatus.FAILED: frozenset(),
    CampaignStatus.CANCELLED: frozenset(),
}

# Statuses from which a recipient still has work outstanding.
_UNSENT = frozenset({RecipientStatus.PENDING, RecipientStatus.QUEUED})


class CampaignStateError(Exception):
    """The requested transition is not allowed from the current state."""

    def __init__(self, current: CampaignStatus, target: CampaignStatus) -> None:
        super().__init__(f"Cannot move a campaign from {current.value} to {target.value}")
        self.current = current
        self.target = target


class TemplateNotApprovedError(Exception):
    """The chosen template is not approved by Meta ([D6])."""


class CampaignNotSendableError(Exception):
    """The campaign cannot be broadcast in its current shape."""


@dataclass(frozen=True, slots=True)
class TemplateFacts:
    """What the campaign rules need to know about a template.

    A plain value rather than the ORM row, so the rules stay testable and the domain
    does not depend on persistence.
    """

    provider_status: str
    is_marketing: bool


def is_terminal(status: CampaignStatus) -> bool:
    return not _TRANSITIONS[status]


def allowed_transitions(status: CampaignStatus) -> frozenset[CampaignStatus]:
    return _TRANSITIONS[status]


def can_transition(current: CampaignStatus, target: CampaignStatus) -> bool:
    return target in _TRANSITIONS[current]


def assert_transition(current: CampaignStatus, target: CampaignStatus) -> None:
    """Raise unless the transition is allowed.

    Same-state moves are rejected too: a no-op transition usually means the caller
    lost track of the state rather than genuinely intending nothing.
    """
    if not can_transition(current, target):
        raise CampaignStateError(current, target)


def assert_template_usable(template: TemplateFacts) -> None:
    """A campaign may only use a template Meta has approved ([D6]).

    Approval is never assigned locally; this compares the value Meta reported. The
    marketing check is the second half of [D6]: a broadcast must use the approved
    template type for marketing messages, and sending a UTILITY template as marketing
    is a policy violation Meta can penalise the number for.
    """
    if template.provider_status != META_STATUS_APPROVED:
        raise TemplateNotApprovedError(
            f"Template status is {template.provider_status!r}; only "
            f"{META_STATUS_APPROVED!r} templates may be used"
        )
    if not template.is_marketing:
        raise TemplateNotApprovedError("A broadcast requires a MARKETING category template")


def assert_sendable(status: CampaignStatus, sendable_recipients: int) -> None:
    """Check a campaign can be broadcast before anything is enqueued.

    Refusing a campaign with no sendable recipients avoids a broadcast that reports
    PROCESSING and then silently completes having done nothing — which looks like a
    delivery failure rather than an empty audience.
    """
    if status is not CampaignStatus.READY:
        raise CampaignNotSendableError(
            f"Only a READY campaign can be sent; this one is {status.value}"
        )
    if sendable_recipients <= 0:
        raise CampaignNotSendableError("This campaign has no recipients left to send to")


def recipient_status_for(*, suppressed: bool) -> RecipientStatus:
    """Decide a recipient's initial status.

    [D16] requires the global suppression list to be consulted before sending. A
    suppressed number is SKIPPED, not FAILED: nothing went wrong, and campaign
    reporting must not describe respecting an opt-out as a delivery failure.
    """
    return RecipientStatus.SKIPPED if suppressed else RecipientStatus.PENDING


def is_unsent(status: RecipientStatus) -> bool:
    """Whether a recipient still has outstanding work.

    Used to make re-sending idempotent: a recipient already sent, failed or skipped
    is not enqueued again.
    """
    return status in _UNSENT
