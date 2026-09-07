"""Audience queries."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.audience import ValidatedRow, ValidationSummary
from app.domain.enums import AudienceStatus, ContactValidationStatus
from app.persistence.models import Audience, AudienceContact, Lead


async def create_with_contacts(
    session: AsyncSession,
    *,
    name: str,
    source_filename: str | None,
    uploaded_by_id: uuid.UUID | None,
    summary: ValidationSummary,
) -> Audience:
    """Store an audience and every parsed row, valid or not.

    Rows are inserted in bulk rather than one at a time: a 50,000-row file would
    otherwise mean 50,000 round trips.
    """
    audience = Audience(
        name=name,
        source_filename=source_filename,
        uploaded_by_id=uploaded_by_id,
        status=AudienceStatus.PENDING_CONFIRMATION,
        total_rows=summary.total,
        valid_rows=summary.valid,
        invalid_rows=summary.invalid,
        duplicate_rows=summary.duplicate,
    )
    session.add(audience)
    await session.flush()

    if summary.rows:
        session.add_all(
            [_to_contact(audience.id, row) for row in summary.rows],
        )
        await session.flush()

    return audience


def _to_contact(audience_id: uuid.UUID, row: ValidatedRow) -> AudienceContact:
    return AudienceContact(
        audience_id=audience_id,
        row_number=row.row_number,
        raw_row=_jsonable(row.raw_row),
        phone_e164=row.phone_e164,
        full_name=row.full_name,
        email=row.email,
        company=row.company,
        designation=row.designation,
        city=row.city,
        validation_status=row.status,
        validation_error=row.error,
    )


def _jsonable(raw_row: dict[str, object]) -> dict[str, object]:
    """Coerce cell values to JSON-storable types.

    Excel yields datetimes and Decimals that JSONB cannot take directly. Preserving
    the original as text keeps §6's "store the original row" honest without inventing
    a type mapping.
    """
    cleaned: dict[str, object] = {}
    for key, value in raw_row.items():
        if value is None or isinstance(value, str | int | float | bool):
            cleaned[key] = value
        else:
            cleaned[key] = str(value)
    return cleaned


async def get_by_id(session: AsyncSession, audience_id: uuid.UUID) -> Audience | None:
    return await session.get(Audience, audience_id)


async def list_audiences(session: AsyncSession) -> Sequence[Audience]:
    result = await session.execute(select(Audience).order_by(Audience.created_at.desc()))
    return result.scalars().all()


async def list_contacts(
    session: AsyncSession,
    audience_id: uuid.UUID,
    *,
    status: ContactValidationStatus | None = None,
    limit: int = 100,
    offset: int = 0,
) -> Sequence[AudienceContact]:
    """Contacts for the preview, optionally filtered to one validation status.

    Paginated because an audience can hold tens of thousands of rows and the preview
    must not try to render them all.
    """
    query = select(AudienceContact).where(AudienceContact.audience_id == audience_id)
    if status is not None:
        query = query.where(AudienceContact.validation_status == status)
    query = query.order_by(AudienceContact.row_number).limit(limit).offset(offset)

    result = await session.execute(query)
    return result.scalars().all()


async def count_contacts(
    session: AsyncSession,
    audience_id: uuid.UUID,
    *,
    status: ContactValidationStatus | None = None,
) -> int:
    query = (
        select(func.count())
        .select_from(AudienceContact)
        .where(AudienceContact.audience_id == audience_id)
    )
    if status is not None:
        query = query.where(AudienceContact.validation_status == status)
    result = await session.execute(query)
    return int(result.scalar_one())


async def promote_valid_contacts_to_leads(session: AsyncSession, audience_id: uuid.UUID) -> int:
    """Upsert the audience's valid contacts into `leads`.

    Runs on confirmation, which is the point §6 calls "audience stored" and the moment
    the audience becomes usable by a campaign.

    Deduplication is by normalized phone ([D11]), so re-uploading a number that already
    exists updates the existing lead rather than creating a second. `ON CONFLICT DO
    UPDATE` does this in one statement, which also makes the operation idempotent —
    confirming twice cannot double-create.

    Every canonical field is carried across ([D25]): promotion must not discard
    company, designation or city.

    Existing contact details are only overwritten when the upload actually carries a
    value, so a sparser later file cannot blank out what an earlier one supplied. A
    lead's `status` is never touched here: it belongs to the conversation lifecycle,
    and re-importing a number must not reset a qualified lead to NEW.
    """
    source = (
        select(
            AudienceContact.phone_e164,
            func.max(AudienceContact.full_name).label("full_name"),
            func.max(AudienceContact.email).label("email"),
            func.max(AudienceContact.company).label("company"),
            func.max(AudienceContact.designation).label("designation"),
            func.max(AudienceContact.city).label("city"),
        )
        .where(
            AudienceContact.audience_id == audience_id,
            AudienceContact.validation_status == ContactValidationStatus.VALID,
            AudienceContact.phone_e164.is_not(None),
        )
        .group_by(AudienceContact.phone_e164)
        .subquery()
    )

    rows = (await session.execute(select(source))).mappings().all()
    if not rows:
        return 0

    statement = pg_insert(Lead).values(
        [
            {
                "phone_e164": row["phone_e164"],
                "full_name": row["full_name"],
                "email": row["email"],
                "company": row["company"],
                "designation": row["designation"],
                "city": row["city"],
                "source_audience_id": audience_id,
            }
            for row in rows
        ]
    )
    statement = statement.on_conflict_do_update(
        index_elements=[Lead.phone_e164],
        set_={
            # coalesce, so a sparser later upload cannot blank out detail an earlier
            # one supplied ([D35]: the audience file is authoritative, but a missing
            # value is not an instruction to erase).
            "full_name": func.coalesce(statement.excluded.full_name, Lead.full_name),
            "email": func.coalesce(statement.excluded.email, Lead.email),
            "company": func.coalesce(statement.excluded.company, Lead.company),
            "designation": func.coalesce(statement.excluded.designation, Lead.designation),
            "city": func.coalesce(statement.excluded.city, Lead.city),
        },
    )
    await session.execute(statement)
    await session.flush()

    await link_contacts_to_leads(session, audience_id)
    return len(rows)


async def link_contacts_to_leads(session: AsyncSession, audience_id: uuid.UUID) -> int:
    """Record which lead each valid contact became ([D55]).

    This is the audience *membership* relationship, and it is what a campaign's
    recipient snapshot reads. It has to be a link per contact rather than a column on
    the lead, because a lead legitimately belongs to several audiences — the same
    number uploaded in two files is one lead in both of them.

    Only VALID rows are linked. INVALID rows have no usable phone, and a DUPLICATE row
    is a repeat of a number within the same file whose first occurrence is already
    VALID and already linked, so linking it again would add nothing.

    Idempotent: re-confirming an audience matches the same rows to the same leads.

    The match is on the normalized phone alone, which is unambiguous only while
    `leads.phone_e164` is globally unique. **Phase 19 ([D41]) makes leads per-product**,
    at which point one phone matches several leads and this join must also carry the
    audience's product. Left as a note rather than written early, because the column it
    would need does not exist yet.
    """
    statement = (
        update(AudienceContact)
        .where(
            AudienceContact.audience_id == audience_id,
            AudienceContact.validation_status == ContactValidationStatus.VALID,
            AudienceContact.phone_e164 == Lead.phone_e164,
        )
        .values(lead_id=Lead.id)
    )
    result = await session.execute(statement)
    await session.flush()
    return int(cast("CursorResult[Any]", result).rowcount or 0)


async def confirm(session: AsyncSession, audience: Audience) -> None:
    audience.status = AudienceStatus.CONFIRMED
    await session.flush()
