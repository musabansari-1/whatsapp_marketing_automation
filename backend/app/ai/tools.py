"""The six authorized tools (specification §9, §12, [D9], [D18]).

§9 is explicit: the AI must not reach arbitrary databases or external APIs, and may
only request the six tools listed there. This module is that boundary. Everything the
model can cause to happen in the backend passes through `dispatch`, which:

- refuses any name outside `AuthorizedTool`, so a hallucinated tool is an error rather
  than an action;
- validates arguments before touching anything, because the model produces plausible
  nonsense as readily as valid input;
- returns a result the model reads as *data*, never as an instruction.

Three rules from CLAUDE.md are enforced here rather than trusted to the prompt:

- **`update_lead` cannot set `status`.** [D18] gives the backend sole authority; the
  tool writes extracted facts into `qualification_data` and the rules decide the rest.
- **A demo is never claimed as booked until Calendar confirms.** `book_demo` returns
  failure when the provider refuses, and the caller must not paper over it.
- **The slot is re-checked immediately before booking** (§12), because the customer
  chose from a list computed minutes ago.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.domain import qualification
from app.domain.conversation import can_transition_lead
from app.domain.enums import BookingStatus, LeadStatus
from app.domain.slots import SlotPolicy, available_slots, is_slot_available
from app.integrations.calendar import (
    CalendarProvider,
    EventRequest,
    SlotUnavailableError,
    TimeInterval,
)
from app.integrations.llm import AuthorizedTool, ToolSpec
from app.persistence.repositories import bookings as booking_repo
from app.persistence.repositories import conversations as conversation_repo

logger = get_logger(__name__)

# How many slots a customer is offered. More than a handful read as a wall of text on
# WhatsApp and stop being a choice.
MAX_OFFERED_SLOTS = 5


class ToolError(Exception):
    """The tool could not run. Reported back to the model as a failed result."""


class UnauthorizedToolError(ToolError):
    """The model asked for a tool outside the §9 set."""


@dataclass(slots=True)
class ToolContext:
    """Everything a tool is allowed to touch.

    Scoped to one conversation on purpose: the model cannot name a different lead and
    read it, because no tool takes a lead ID. That is the difference between an
    authorized tool and database access.
    """

    session: AsyncSession
    lead: Any
    conversation: Any
    slot_policy: SlotPolicy
    calendar: CalendarProvider
    qualification_rules: qualification.QualificationRules
    now: datetime | None = None
    # Filled by tools whose effects the caller must act on after the turn.
    booked_event_id: str | None = None
    booking_id: uuid.UUID | None = None
    # Set only when a booking was newly recorded, so a retried turn does not notify
    # the team twice about the same demo.
    notify_booking_id: uuid.UUID | None = None
    proposed_status: Any = None


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What the model gets back.

    `ok` is explicit so a failure cannot read as a success with odd content — the
    difference between "the demo is booked" and "the slot was taken" must not be a
    matter of interpretation.
    """

    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def as_payload(self) -> dict[str, Any]:
        if self.ok:
            return {"ok": True, **self.data}
        return {"ok": False, "error": self.error or "the tool failed"}


# ---------------------------------------------------------------- specifications


def tool_specs() -> tuple[ToolSpec, ...]:
    """The tools offered to the model.

    Descriptions are written for the model and matter as much as the schemas: they are
    where "do not promise a booking you have not made" is stated in terms it acts on.
    """
    return (
        ToolSpec(
            name=AuthorizedTool.GET_LEAD,
            description=(
                "Get what is known about the customer you are talking to, including "
                "information already collected earlier in this conversation."
            ),
            parameters={"type": "object", "properties": {}, "required": []},
        ),
        ToolSpec(
            name=AuthorizedTool.UPDATE_LEAD,
            description=(
                "Record facts the customer has told you. Only record what they "
                "actually said; do not guess. You cannot set the lead's status — the "
                "system decides that from what you record."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "full_name": {"type": "string"},
                    "email": {"type": "string"},
                    "company": {"type": "string"},
                    "designation": {"type": "string"},
                    "city": {"type": "string"},
                    "facts": {
                        "type": "object",
                        "description": (
                            "Structured facts extracted from the conversation, such as "
                            "interested, demo_intent, not_interested, team_size, budget "
                            "or use_case."
                        ),
                        "additionalProperties": True,
                    },
                },
                "required": [],
            },
        ),
        ToolSpec(
            name=AuthorizedTool.GET_CONVERSATION,
            description="Get the earlier turns of this conversation.",
            parameters={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "How many recent turns to return.",
                    }
                },
                "required": [],
            },
        ),
        ToolSpec(
            name=AuthorizedTool.GET_AVAILABLE_SLOTS,
            description=(
                "Get demo times that are actually free. Only offer times this returns. "
                "Never invent a time or promise one you have not checked."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": f"How many slots to return, at most {MAX_OFFERED_SLOTS}.",
                    }
                },
                "required": [],
            },
        ),
        ToolSpec(
            name=AuthorizedTool.BOOK_DEMO,
            description=(
                "Book a demo at a time the customer chose from get_available_slots. "
                "Only tell the customer the demo is booked if this returns ok: true. "
                "If it fails, the slot was taken and you must offer another."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "start_time": {
                        "type": "string",
                        "description": "Slot start in ISO 8601 format, exactly as returned by get_available_slots.",
                    }
                },
                "required": ["start_time"],
            },
        ),
        ToolSpec(
            name=AuthorizedTool.SEND_WHATSAPP_MESSAGE,
            description=(
                "Not needed for a normal reply — your reply is sent automatically. "
                "This exists only for the rare case of sending a second, separate "
                "message."
            ),
            parameters={
                "type": "object",
                "properties": {"body": {"type": "string"}},
                "required": ["body"],
            },
        ),
    )


# ---------------------------------------------------------------- dispatch


async def dispatch(name: str, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    """Run one tool request from the model.

    An unrecognized name is refused rather than guessed at. Models hallucinate tool
    names, and a fuzzy match would run something the customer's conversation never
    called for.
    """
    try:
        tool = AuthorizedTool(name)
    except ValueError:
        logger.warning("ai_unauthorized_tool_refused", tool=name)
        return ToolResult(
            ok=False, error=f"{name!r} is not an available tool. Use only the tools provided."
        )

    handler = _HANDLERS.get(tool)
    if handler is None:
        return ToolResult(ok=False, error=f"{tool.value!r} is not implemented yet")

    try:
        result = await handler(arguments, context)
    except ToolError as exc:
        return ToolResult(ok=False, error=str(exc))

    logger.info("ai_tool_executed", tool=tool.value, ok=result.ok)
    return result


# ---------------------------------------------------------------- handlers


async def _get_lead(_arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    lead = context.lead
    return ToolResult(
        ok=True,
        data={
            "name": lead.full_name,
            "email": lead.email,
            "company": lead.company,
            "designation": lead.designation,
            "city": lead.city,
            # The status is readable but not writable: the model may reason about
            # where the lead stands without being able to move it ([D18]).
            "status": lead.status.value,
            "known_facts": dict(lead.qualification_data or {}),
        },
    )


async def _update_lead(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    """Record what the customer said.

    Note what is missing: `status`. [D18] makes the backend solely responsible for it,
    so the tool has no parameter for it and the schema above does not offer one. The
    facts recorded here are what the qualification rules then read.
    """
    lead = context.lead

    await conversation_repo.update_lead_details(
        context.session,
        lead,
        full_name=_clean(arguments.get("full_name")),
        email=_clean(arguments.get("email")),
        company=_clean(arguments.get("company")),
        designation=_clean(arguments.get("designation")),
        city=_clean(arguments.get("city")),
    )

    facts = arguments.get("facts")
    if isinstance(facts, dict) and facts:
        await conversation_repo.merge_qualification_data(context.session, lead, facts)

    # The rules run on the merged facts, and their conclusion is proposed rather than
    # applied: the caller decides, after the turn, through the §10 table.
    outcome = qualification.evaluate(
        current=lead.status,
        data=dict(lead.qualification_data or {}),
        rules=context.qualification_rules,
    )
    context.proposed_status = outcome.status

    return ToolResult(
        ok=True,
        data={
            "recorded": True,
            # Told to the model so it knows what still needs asking — this is what
            # turns the configured field list into a conversation that collects it.
            "still_needed": sorted(outcome.missing_fields),
        },
    )


async def _get_conversation(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    limit = _positive_int(arguments.get("limit"), default=20, maximum=100)
    turns = await conversation_repo.get_history(
        context.session, context.conversation.id, limit=limit
    )
    return ToolResult(
        ok=True,
        data={"turns": [{"role": turn.role, "content": turn.content} for turn in turns]},
    )


async def _get_available_slots(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    """Offer real availability (§12).

    The busy intervals come from the calendar provider, so the slots reflect the sales
    team's actual diary rather than a guess. §12's sequence starts here: retrieve real
    slots, then show those slots, and never invent one.
    """
    limit = _positive_int(
        arguments.get("limit"), default=MAX_OFFERED_SLOTS, maximum=MAX_OFFERED_SLOTS
    )
    now = context.now or datetime.now(UTC)
    horizon = TimeInterval(start=now, end=now + _days(context.slot_policy.horizon_days))

    try:
        busy = await context.calendar.list_busy_intervals(horizon)
    except Exception as exc:  # noqa: BLE001 - any calendar failure is the same to the model
        # Reported as a tool failure rather than raised: the model can say it will
        # follow up, which is better than the whole turn failing.
        logger.warning("ai_slot_lookup_failed", error=str(exc))
        raise ToolError("Could not check the calendar just now") from exc

    slots = available_slots(policy=context.slot_policy, busy=busy, now=now, limit=limit)

    return ToolResult(
        ok=True,
        data={
            "timezone": context.slot_policy.timezone,
            "duration_minutes": context.slot_policy.duration_minutes,
            "slots": [
                {
                    "start_time": slot.start.isoformat(),
                    "local_time": slot.start.astimezone(context.slot_policy.zone).strftime(
                        "%A %d %B, %I:%M %p"
                    ),
                }
                for slot in slots
            ],
        },
    )


async def _book_demo(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    """Book a demo, and never claim one that Calendar did not confirm.

    The §12 sequence in full: re-check the slot against live busy intervals, create the
    event, and only then report success. The re-check is not redundant — the customer
    chose from a list computed minutes earlier, and someone else may have taken it
    since.

    The already-booked check comes *first*, before the re-check, and that ordering is
    load-bearing. On a retried turn the booking's own calendar event is now busy time,
    so re-checking first would find the slot occupied and tell the customer their demo
    was lost — when in fact it is theirs.
    """
    start = _parse_start_time(arguments.get("start_time"))
    now = context.now or datetime.now(UTC)
    slot = TimeInterval(start=start, end=start + _minutes(context.slot_policy.duration_minutes))

    # Deterministic in the conversation and the slot, so a retried turn recognises its
    # own earlier success rather than treating it as somebody else's booking.
    event_id = f"demo-{context.conversation.id}-{int(start.timestamp())}"

    already = await booking_repo.find_by_event_id(context.session, event_id)
    if already is not None and already.status is BookingStatus.CONFIRMED:
        context.booked_event_id = event_id
        context.booking_id = already.id
        # Deliberately not set: the team was notified when this booking was first
        # recorded, and telling them twice about one demo is noise.
        logger.info("ai_demo_already_booked", booking_id=str(already.id))
        return ToolResult(
            ok=True,
            data={
                "booked": True,
                "event_id": event_id,
                "start_time": already.start_time.isoformat(),
                "local_time": already.start_time.astimezone(context.slot_policy.zone).strftime(
                    "%A %d %B, %I:%M %p"
                ),
                "timezone": already.timezone,
            },
        )

    horizon = TimeInterval(start=now, end=now + _days(context.slot_policy.horizon_days))
    try:
        busy = await context.calendar.list_busy_intervals(horizon)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ai_booking_precheck_failed", error=str(exc))
        raise ToolError("Could not confirm the calendar just now") from exc

    if not is_slot_available(policy=context.slot_policy, slot=slot, busy=busy, now=now):
        return ToolResult(
            ok=False,
            error="That time is no longer available. Offer the customer another slot.",
        )

    try:
        event = await context.calendar.create_event(
            EventRequest(
                event_id=event_id,
                interval=slot,
                timezone=context.slot_policy.timezone,
                summary=f"Demo — {context.lead.full_name or context.lead.phone_e164}",
                description=_booking_description(context.lead),
                attendee_emails=(context.lead.email,) if context.lead.email else (),
                metadata={"lead_id": str(context.lead.id)},
            )
        )
    except SlotUnavailableError:
        # Lost the race between the re-check and the write. Reported honestly.
        return ToolResult(
            ok=False,
            error="That time was taken while booking. Offer the customer another slot.",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("ai_booking_failed", error=str(exc))
        raise ToolError("The booking could not be completed") from exc

    context.booked_event_id = event.event_id

    # Record the booking (§12). The calendar event already exists, so this cannot be
    # skipped on failure: an event with no row is a demo the dashboard does not know
    # about. `record_confirmed_booking` is keyed on the event ID, so a retry that
    # reaches an already-created event converges on the existing row.
    booking, created = await booking_repo.record_confirmed_booking(
        context.session,
        lead=context.lead,
        conversation_id=context.conversation.id,
        event=event,
    )
    context.booking_id = booking.id

    if created:
        # The lead has a demo. Proposed through the §10 table like every other status
        # change — a booking is a fact, but the transition still has to be legal.
        if can_transition_lead(context.lead.status, LeadStatus.DEMO_BOOKED):
            context.proposed_status = LeadStatus.DEMO_BOOKED

        # Notifying the team is deliberately *not* inline. It fans out to every active
        # user ([D3]) and its failure must not delay — or undo — a booking the
        # customer is waiting to hear about. §12 is explicit that a confirmed booking
        # stays confirmed while a failed notification retries.
        context.notify_booking_id = booking.id

    logger.info(
        "ai_demo_booked",
        event_id=event.event_id,
        booking_id=str(booking.id),
        lead_id=str(context.lead.id),
        newly_created=created,
    )

    return ToolResult(
        ok=True,
        data={
            "booked": True,
            "event_id": event.event_id,
            "start_time": event.interval.start.isoformat(),
            "local_time": event.interval.start.astimezone(context.slot_policy.zone).strftime(
                "%A %d %B, %I:%M %p"
            ),
            "timezone": context.slot_policy.timezone,
        },
    )


async def _send_whatsapp_message(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    """Refused in V1.

    §9 lists the tool, but the turn handler already sends the model's reply through
    the window and suppression checks. A second sending path would be a way around
    them, so the tool exists, is authorized, and declines — which the model reads and
    puts its text in the reply instead.
    """
    body = _clean(arguments.get("body"))
    if not body:
        return ToolResult(ok=False, error="A message body is required")

    logger.info("ai_send_message_tool_declined", conversation_id=str(context.conversation.id))
    return ToolResult(
        ok=False,
        error=(
            "Messages are sent automatically as your reply. Put what you want to say "
            "in your reply instead of using this tool."
        ),
    )


_HANDLERS: dict[AuthorizedTool, Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]] = {
    AuthorizedTool.GET_LEAD: _get_lead,
    AuthorizedTool.UPDATE_LEAD: _update_lead,
    AuthorizedTool.GET_CONVERSATION: _get_conversation,
    AuthorizedTool.GET_AVAILABLE_SLOTS: _get_available_slots,
    AuthorizedTool.BOOK_DEMO: _book_demo,
    AuthorizedTool.SEND_WHATSAPP_MESSAGE: _send_whatsapp_message,
}


# ---------------------------------------------------------------- helpers


def _booking_description(lead: Any) -> str:
    parts = [f"Demo booked from a WhatsApp conversation with {lead.phone_e164}."]
    if lead.company:
        parts.append(f"Company: {lead.company}")
    if lead.designation:
        parts.append(f"Role: {lead.designation}")
    return "\n".join(parts)


def _parse_start_time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ToolError("start_time is required, in ISO 8601 format")

    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ToolError(f"{value!r} is not a valid ISO 8601 time") from exc

    if parsed.tzinfo is None:
        # A naive time is ambiguous, and guessing a timezone here would book the demo
        # in the wrong one. Refused so the model re-reads the slot it was given.
        raise ToolError(
            "start_time must include a timezone offset, exactly as get_available_slots returned it"
        )
    return parsed


def _positive_int(value: Any, *, default: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return default
    return min(value, maximum)


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _minutes(count: int) -> timedelta:
    return timedelta(minutes=count)


def _days(count: int) -> timedelta:
    return timedelta(days=count)


__all__ = [
    "MAX_OFFERED_SLOTS",
    "ToolContext",
    "ToolError",
    "ToolResult",
    "UnauthorizedToolError",
    "dispatch",
    "tool_specs",
]
