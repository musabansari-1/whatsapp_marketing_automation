"""Request and response models.

Separate from the ORM models on purpose: `password_hash` must never be serializable
into a response, and a request body must never be able to set `role` or `is_active`
on a path that does not intend it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.domain.enums import (
    AudienceStatus,
    CampaignStatus,
    ContactValidationStatus,
    RecipientStatus,
    TemplateCategory,
    UserRole,
)

# Long enough to resist guessing, capped because Argon2 hashes the whole input and an
# unbounded body would be a cheap denial-of-service.
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class UserResponse(BaseModel):
    """A user as the API exposes it. Note the absence of `password_hash`."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str | None
    role: UserRole
    is_active: bool
    created_at: datetime


class CreateUserRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)
    full_name: str | None = Field(default=None, max_length=255)
    role: UserRole

    @field_validator("password")
    @classmethod
    def _reject_whitespace_only(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("password cannot be blank")
        return value


class UpdateUserRequest(BaseModel):
    """Every field optional; omitted fields are left unchanged.

    `password` is accepted here so an admin can reset one, but the current password is
    never returned or echoed.
    """

    full_name: str | None = Field(default=None, max_length=255)
    role: UserRole | None = None
    is_active: bool | None = None
    password: str | None = Field(
        default=None, min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH
    )


class MessageResponse(BaseModel):
    detail: str


# ---------------------------------------------------------------- audiences (§6)


class AudienceSummaryResponse(BaseModel):
    """An audience in list and detail views, with the §6 preview counts."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    source_filename: str | None
    status: AudienceStatus
    total_rows: int
    valid_rows: int
    invalid_rows: int
    duplicate_rows: int
    created_at: datetime


class AudienceContactResponse(BaseModel):
    """One parsed row. Invalid and duplicate rows are included by design (§6)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    row_number: int
    full_name: str | None
    phone_e164: str | None
    email: str | None
    company: str | None
    designation: str | None
    city: str | None
    validation_status: ContactValidationStatus
    validation_error: str | None
    raw_row: dict[str, Any]


class AudienceContactPage(BaseModel):
    """A page of contacts; an audience can hold tens of thousands of rows."""

    total: int
    limit: int
    offset: int
    items: list[AudienceContactResponse]


class AudienceUploadResponse(BaseModel):
    """The preview returned by upload.

    The audience is stored but unconfirmed: §6 requires an admin to review the preview
    before it can be used, and nothing is sent as a result of uploading.
    """

    audience: AudienceSummaryResponse
    sample: list[AudienceContactResponse]


class AudienceConfirmResponse(BaseModel):
    audience: AudienceSummaryResponse
    leads_upserted: int


class AudienceSchemaResponse(BaseModel):
    """The configured column schema, so the UI can tell the user what file to upload.

    `is_placeholder` surfaces that the client's real schema has not been supplied yet
    ([D4]/[D20]) rather than letting a development default look authoritative.
    """

    columns: list[dict[str, Any]]
    is_placeholder: bool
    phone_default_region: str
    max_file_bytes: int
    max_rows: int


# ---------------------------------------------------------------- campaigns (§7)


class WhatsAppTemplateResponse(BaseModel):
    """A template as synchronized from Meta ([D6]).

    `provider_status` is Meta's own value, carried through unchanged. Nothing in this
    application assigns approval.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    language: str
    category: TemplateCategory
    provider_status: str
    variable_count: int
    body_preview: str | None


class CreateCampaignRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    audience_id: uuid.UUID
    template_id: uuid.UUID


class CampaignResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    status: CampaignStatus
    audience_id: uuid.UUID
    template_id: uuid.UUID
    total_recipients: int
    sent_count: int
    delivered_count: int
    read_count: int
    failed_count: int
    skipped_count: int
    scheduled_for: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class CampaignDetailResponse(BaseModel):
    """A campaign with the template it will send and its outstanding work.

    `pending_recipients` is what the Send Broadcast confirmation is about, so the
    admin sees the number before triggering it.
    """

    campaign: CampaignResponse
    template: WhatsAppTemplateResponse
    pending_recipients: int


class CampaignRecipientResponse(BaseModel):
    """One frozen recipient (§7 snapshot)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    lead_id: uuid.UUID
    phone_e164: str
    status: RecipientStatus
    attempts: int
    provider_message_id: str | None
    error_message: str | None


class CampaignRecipientPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[CampaignRecipientResponse]


class SendCampaignResponse(BaseModel):
    """The result of confirming Send Broadcast.

    `queued` counts tasks handed to the queue. Delivery happens asynchronously in the
    worker; nothing is sent inside this request (§4).
    """

    campaign: CampaignResponse
    queued: int
    skipped_as_suppressed: int
