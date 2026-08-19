"""Excel audience endpoints (specification §6, §16, [D4], [D20]).

Uploading validates and stores; it never sends a message and never invokes the AI.
That is a hard rule in §6 and CLAUDE.md, and it is why this module imports no
provider: there is nothing here that *could* send.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, status

from app.api.deps import CurrentUser, DbSession, SettingsDep
from app.api.schemas import (
    AudienceConfirmResponse,
    AudienceContactPage,
    AudienceContactResponse,
    AudienceSchemaResponse,
    AudienceSummaryResponse,
    AudienceUploadResponse,
)
from app.core.logging import get_logger
from app.domain.audience import MissingColumnsError, find_missing_columns, validate_rows
from app.domain.enums import AudienceStatus, ContactValidationStatus
from app.domain.excel import ExcelParseError, TooManyRowsError, parse_excel
from app.persistence.repositories import audiences as audience_repo

router = APIRouter(prefix="/audiences", tags=["audiences"])
logger = get_logger(__name__)

# Rows returned inline with the upload preview. The full set is paginated through
# GET /audiences/{id}/contacts.
PREVIEW_SAMPLE_SIZE = 25

XLSX_CONTENT_TYPES = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "application/octet-stream",
}


@router.get("/schema", response_model=AudienceSchemaResponse)
async def get_schema(user: CurrentUser, settings: SettingsDep) -> AudienceSchemaResponse:
    """The column schema uploads are validated against.

    Exposed so the dashboard can state the expected format instead of the user
    guessing, and so it can warn while the development placeholder is still in force.
    """
    schema = settings.audience_schema()
    return AudienceSchemaResponse(
        columns=[
            {"source": column.source, "field": column.field.value, "required": column.required}
            for column in schema.columns
        ],
        is_placeholder=settings.audience_schema_is_placeholder,
        phone_default_region=settings.audience_phone_default_region,
        max_file_bytes=settings.audience_max_file_bytes,
        max_rows=settings.audience_max_rows,
    )


@router.post("/upload", response_model=AudienceUploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_audience(
    user: CurrentUser,
    session: DbSession,
    settings: SettingsDep,
    file: UploadFile = File(...),
    name: str | None = Form(default=None),
) -> AudienceUploadResponse:
    """Validate a spreadsheet and store it as an unconfirmed audience.

    The response is the §6 preview. Nothing is sent and no AI runs; the audience is
    inert until an admin confirms it.
    """
    content = await _read_within_limit(file, settings.audience_max_file_bytes)
    schema = settings.audience_schema()

    try:
        sheet = parse_excel(content, max_rows=settings.audience_max_rows)
    except TooManyRowsError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)
        ) from exc
    except ExcelParseError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    missing = find_missing_columns(sheet.headers, schema)
    if missing:
        # A missing required column is the wrong file, not bad data, so the upload is
        # rejected whole rather than producing one identical error per row.
        error = MissingColumnsError(missing)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error))

    summary = validate_rows(
        sheet.rows,
        schema=schema,
        default_region=settings.audience_phone_default_region,
    )
    if summary.total == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The file contains headers but no data rows",
        )

    audience = await audience_repo.create_with_contacts(
        session,
        name=(name or "").strip() or (file.filename or "Untitled audience"),
        source_filename=file.filename,
        uploaded_by_id=user.id,
        summary=summary,
    )

    logger.info(
        "audience_uploaded",
        audience_id=str(audience.id),
        total_rows=summary.total,
        valid_rows=summary.valid,
        invalid_rows=summary.invalid,
        duplicate_rows=summary.duplicate,
    )

    sample = await audience_repo.list_contacts(session, audience.id, limit=PREVIEW_SAMPLE_SIZE)
    return AudienceUploadResponse(
        audience=AudienceSummaryResponse.model_validate(audience),
        sample=[AudienceContactResponse.model_validate(contact) for contact in sample],
    )


@router.get("", response_model=list[AudienceSummaryResponse])
async def list_audiences(user: CurrentUser, session: DbSession) -> list[AudienceSummaryResponse]:
    audiences = await audience_repo.list_audiences(session)
    return [AudienceSummaryResponse.model_validate(audience) for audience in audiences]


@router.get("/{audience_id}", response_model=AudienceSummaryResponse)
async def get_audience(
    audience_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> AudienceSummaryResponse:
    audience = await _require_audience(session, audience_id)
    return AudienceSummaryResponse.model_validate(audience)


@router.get("/{audience_id}/contacts", response_model=AudienceContactPage)
async def list_contacts(
    audience_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    validation_status: ContactValidationStatus | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> AudienceContactPage:
    """Paged contacts, filterable by validation status.

    The filter is what makes the preview useful: an admin reviewing a rejected upload
    wants the invalid rows, not the 9,000 that were fine.
    """
    await _require_audience(session, audience_id)

    contacts = await audience_repo.list_contacts(
        session, audience_id, status=validation_status, limit=limit, offset=offset
    )
    total = await audience_repo.count_contacts(session, audience_id, status=validation_status)

    return AudienceContactPage(
        total=total,
        limit=limit,
        offset=offset,
        items=[AudienceContactResponse.model_validate(contact) for contact in contacts],
    )


@router.post("/{audience_id}/confirm", response_model=AudienceConfirmResponse)
async def confirm_audience(
    audience_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> AudienceConfirmResponse:
    """Confirm the audience after reviewing the preview (§6).

    This endpoint is derived: §6 requires an admin confirmation step but §16 does not
    list a route for it.

    Confirming upserts the valid contacts into `leads`, deduplicated by normalized
    phone ([D11]). Still no message is sent — a campaign is a separate, explicitly
    triggered action (§7).
    """
    audience = await _require_audience(session, audience_id)

    if audience.status is AudienceStatus.CONFIRMED:
        # Idempotent: re-confirming is a no-op rather than an error, so a double
        # click cannot produce a confusing failure.
        total = await audience_repo.count_contacts(
            session, audience_id, status=ContactValidationStatus.VALID
        )
        return AudienceConfirmResponse(
            audience=AudienceSummaryResponse.model_validate(audience), leads_upserted=total
        )

    if audience.valid_rows == 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="This audience has no valid contacts to confirm",
        )

    upserted = await audience_repo.promote_valid_contacts_to_leads(session, audience_id)
    await audience_repo.confirm(session, audience)

    logger.info(
        "audience_confirmed",
        audience_id=str(audience.id),
        leads_upserted=upserted,
        by_user_id=str(user.id),
    )
    return AudienceConfirmResponse(
        audience=AudienceSummaryResponse.model_validate(audience), leads_upserted=upserted
    )


async def _require_audience(session: DbSession, audience_id: uuid.UUID):  # type: ignore[no-untyped-def]
    audience = await audience_repo.get_by_id(session, audience_id)
    if audience is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audience not found")
    return audience


async def _read_within_limit(file: UploadFile, max_bytes: int) -> bytes:
    """Read the upload, refusing anything over the limit (§19).

    Checked against the bytes actually read rather than the declared Content-Length,
    which a client controls and can understate.
    """
    if file.filename and not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Only .xlsx files are supported",
        )
    if file.content_type and file.content_type not in XLSX_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported content type: {file.content_type}",
        )

    content = await file.read()
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds the maximum size of {max_bytes} bytes",
        )
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The file is empty")
    return content
