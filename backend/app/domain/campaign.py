"""Campaign rules and state machine (specification §7, [D6], [D16]).

Pure logic: no database, no HTTP. The transition table is the authority on what a
campaign may do next, so the API cannot move a campaign somewhere the specification
does not allow just because a handler forgot to check.

§7 lists the eight states but not the edges between them. Those are derived here and
each is justified, so review can challenge any single edge rather than the whole
table.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.domain.enums import CampaignStatus, RecipientStatus, TemplateHeaderType

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


class TemplateOwnershipError(Exception):
    """The template is not owned by the campaign's product (§28.8, [D57])."""


class TemplateNotFoundError(Exception):
    """No template row exists for the given id."""


class TemplateAssignmentError(Exception):
    """A template cannot be given to that product ([D57])."""


def assert_template_owned_by(
    *, template_product_id: uuid.UUID | None, campaign_product_id: uuid.UUID
) -> None:
    """A campaign may use a template only when its own product explicitly owns it ([D57]).

    Two refusals, and the first is the one that matters more. One Meta App and one
    shared WABA means template sync returns every product's templates in a single pool
    with no owner recorded, so a newly discovered template arrives with no product. That
    is a provider artifact nobody has claimed — not a template every product may use.
    Treating NULL as "available to all" would let one product broadcast another's
    approved wording from its own number.

    Ownership is compared, never inferred: not from the name, the WABA, the number, the
    sole active product or campaign history.
    """
    if template_product_id is None:
        raise TemplateOwnershipError(
            "That template has not been assigned to a product yet, so no campaign can use it"
        )
    if template_product_id != campaign_product_id:
        raise TemplateOwnershipError("That template belongs to a different product")


def needs_assignment(*, current_owner: uuid.UUID | None, target: uuid.UUID) -> bool:
    """Whether giving a template to `target` changes anything ([D57]).

    Unassigned → assign. Already `target`'s → nothing to do, so a repeated request is
    harmless. Already another product's → refused: one provider template has at most one
    owner, and moving it would silently change what that product's existing campaigns
    are allowed to send. Sharing one template across products is a separate requirement
    that would need a mapping table, and nothing asks for it yet.
    """
    if current_owner is None:
        return True
    if current_owner == target:
        return False
    raise TemplateAssignmentError("That template already belongs to another product")


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


class TemplateMediaMissingError(Exception):
    """The template's header needs media, and none has been chosen for it."""


def assert_header_media_available(
    *, header_type: TemplateHeaderType, media_url: str | None
) -> None:
    """Refuse a template whose media header has no asset to send.

    Meta validates the components of a send against the approved template and refuses a
    mismatch permanently (132012, "header: Format mismatch, expected IMAGE, received
    UNKNOWN"). Omitting the header to avoid that mismatch is not an option — it is the
    same rejection — and retrying never helps, because no attempt can supply media the
    platform does not have.

    So a media-header template with no URL is caught here, where the operator is told
    which template and what is missing, instead of once per recipient at send time,
    where the same permanent failure would be reported N times and look like a
    provider outage. A TEXT or absent header needs nothing and always passes.
    """
    if header_type.needs_media and not media_url:
        raise TemplateMediaMissingError(
            f"This template has a {header_type.value} header, so it needs a media URL "
            "before any campaign can send it"
        )


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
