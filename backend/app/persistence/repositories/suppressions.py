"""Global opt-out, and keeping every lead's mirror honest (§28.7, [D42]).

`whatsapp_suppressions` is the authoritative record of who may not be marketed to. It
is global at the normalized phone level across every product — [D5], [D16], [D26] and
[D42] all say so, and Phase 19's per-product leads did not change it. There is no
`product_id` on that table and there must never be one.

`leads.opted_out` and `leads.status` are a **mirror** for display and for the CRM. They
are never the check that decides whether a message may be sent; §7 checks the table, at
snapshot and again at send.

The mirror only became hard to keep true when one human started having several lead rows
([D41]). A STOP arrives through one product's conversation, but consent is a property of
the person, so it has to reach every row sharing that number — and a lead created
*after* the opt-out has to arrive already suppressed rather than waiting for something to
notice. This module is the single place that does both.

**The invariant:** no lead creation, upsert or adoption path may return a lead whose
`opted_out` mirror contradicts the authoritative suppression state for its phone.

**`INVALID` is not overwritten**, deliberately. The two facts are different and both are
worth keeping:

    INVALID                      a delivery finding: Meta says this number is unreachable
    opted_out / suppression row  a consent fact: this person asked not to be contacted

A suppressed `INVALID` lead records consent on its mirror and keeps its status. Sending
is unaffected, because **consent never comes from the status enum**: audited in Phase 20
and asserted by test — `campaign_send` and the conversation turn both check this table,
and no send path treats `status != OPTED_OUT` as permission to message.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import CursorResult, and_, case, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import LeadStatus
from app.persistence.models import Lead, WhatsAppSuppression

#: Lead states the §10 table makes terminal, whose status opt-out must not overwrite.
#:
#: `OPTED_OUT` is already the destination. `INVALID` records a number Meta rejected as
#: unreachable, and rewriting it would discard that finding to say something the
#: suppression table already says better. The `opted_out` mirror is still set on both —
#: consent is recorded either way, and only the status is left alone.
_TERMINAL = (LeadStatus.OPTED_OUT, LeadStatus.INVALID)


async def is_suppressed(session: AsyncSession, phone_e164: str) -> bool:
    """Whether this number has opted out, anywhere, for any product."""
    result = await session.execute(
        select(WhatsAppSuppression.id).where(WhatsAppSuppression.phone_e164 == phone_e164)
    )
    return result.scalars().first() is not None


async def enforce_mirror(session: AsyncSession, phones: Sequence[str]) -> int:
    """Make every lead for these numbers agree with the suppression table.

    Applies to **all** lead rows sharing a suppressed number, whatever product they
    belong to and whenever they were created — including the transitional rows with no
    product yet ([D41]'s Phase 19→23 window). Product is irrelevant to consent.

    Returns how many rows it had to correct, so a caller can tell repair from routine.

    **The one sanctioned bypass of `transition_lead`.** CLAUDE.md requires every
    `lead.status` change to go through the §10 transition table, and this does not.
    The exception is narrow and deliberate:

        Global consent enforcement may force a lead into the terminal opt-out state
        directly, because opt-out processing must be idempotent and authoritative.

    Going through `transition_lead` would refuse a repeated STOP — `OPTED_OUT` is
    terminal, so `assert_lead_transition` rejects `OPTED_OUT → OPTED_OUT` — and would
    make the second STOP an error instead of a no-op. Consent enforcement cannot be
    allowed to fail because it already succeeded.

    This is not a general permission. It applies to this function only, it moves leads
    in exactly one direction, and it still respects the §10 table's terminal states by
    leaving their status alone.
    """
    if not phones:
        return 0

    suppressed = (
        select(WhatsAppSuppression.phone_e164)
        .where(WhatsAppSuppression.phone_e164.in_(list(phones)))
        .scalar_subquery()
    )

    # Skip rows that already agree, so the count means something and a repeated STOP
    # writes nothing.
    needs_correction = or_(
        Lead.opted_out.is_(False),
        and_(*(Lead.status != terminal for terminal in _TERMINAL)),
    )

    result = await session.execute(
        update(Lead)
        .where(Lead.phone_e164.in_(suppressed), needs_correction)
        .values(
            opted_out=True,
            status=case(
                (Lead.status.in_(_TERMINAL), Lead.status),
                else_=LeadStatus.OPTED_OUT,
            ),
        )
    )
    await session.flush()
    return int(cast("CursorResult[Any]", result).rowcount or 0)


async def enforce_mirror_for(session: AsyncSession, phone_e164: str) -> int:
    """`enforce_mirror` for one number, which is what most callers have."""
    return await enforce_mirror(session, [phone_e164])


__all__ = ["enforce_mirror", "enforce_mirror_for", "is_suppressed"]
