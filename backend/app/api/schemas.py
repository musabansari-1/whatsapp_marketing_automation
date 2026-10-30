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
    BookingStatus,
    CampaignStatus,
    ContactValidationStatus,
    ConversationStatus,
    DocumentStatus,
    LeadStatus,
    MessageDirection,
    NotificationType,
    RecipientStatus,
    TemplateCategory,
    TemplateHeaderType,
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


# ---------------------------------------------------------------- products (§28)


class SegmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    display_name: str
    display_order: int
    is_active: bool


class ProductResponse(BaseModel):
    """A product as the switcher lists it. No channel identifier or secret is exposed."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    display_name: str
    is_active: bool


class ProductDetailResponse(ProductResponse):
    """One product, with what the dashboard needs to explain itself.

    `has_calendar` and `has_whatsapp_number` are presence flags only, so an empty Demos
    page can say "no calendar is configured" instead of showing an unexplained zero.
    `has_meeting_url` says the same for time-only confirmations: without the product's
    permanent meeting link, demos book with no join link.
    """

    segments: list[SegmentResponse]
    has_calendar: bool
    has_whatsapp_number: bool
    has_meeting_url: bool = False
    # Whether a first-reply welcome template is selected. False is the placeholder
    # state: first-time senders get the normal AI reply until a real template is set.
    has_first_reply_template: bool = False


class ProductRef(BaseModel):
    """Enough to name a product and link to it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    display_name: str


# ---------------------------------------------------------------- audiences (§6)


class AudienceSummaryResponse(BaseModel):
    """An audience in list and detail views, with the §6 preview counts."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    product_id: uuid.UUID
    segment_id: uuid.UUID | None
    name: str
    source_filename: str | None
    status: AudienceStatus
    total_rows: int
    valid_rows: int
    invalid_rows: int
    duplicate_rows: int
    created_at: datetime
    archived_at: datetime | None = None


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
    """A product's column schema, so the UI can tell the user what file to upload.

    `configured` is false when the product's uploads would be refused — no schema, an
    invalid one, or the placeholder in production — and `problem` says why. `is_placeholder`
    surfaces that the real spreadsheet format has not been confirmed yet ([D4]/[D20]) rather
    than letting a development default look authoritative.
    """

    configured: bool
    problem: str | None
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
    # NULL: an unassigned shared-WABA template, usable by no campaign ([D57]).
    product_id: uuid.UUID | None
    name: str
    language: str
    category: TemplateCategory
    provider_status: str
    variable_count: int
    body_preview: str | None
    # The header Meta's template carries, and the media chosen for it. `header_type`
    # is synchronized; `header_media_url` is set explicitly, because Meta's own sample
    # is a placeholder rather than the asset a campaign should send. A media header
    # with no URL is why a campaign would be refused.
    header_type: TemplateHeaderType
    header_media_url: str | None


class ProductTemplateResponse(WhatsAppTemplateResponse):
    """A template a product owns, and whether its campaigns could use it right now.

    `usable_for_campaigns` applies the same rules campaign creation enforces — approved
    by Meta, MARKETING, owned by this product — so the picker and the refusal agree.
    """

    usable_for_campaigns: bool


class SetTemplateHeaderMediaRequest(BaseModel):
    """Choose the media a template's header carries.

    `media_url` is nullable on purpose: sending null clears it, which leaves a
    media-header template unsendable and is refused by name at campaign creation. That
    is preferable to silently keeping an asset the operator believed they had removed.
    """

    media_url: str | None = Field(default=None, max_length=2048)


class TemplateSyncAccepted(BaseModel):
    queued: bool


class CreateCampaignRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    audience_id: uuid.UUID
    template_id: uuid.UUID


class CampaignResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    product_id: uuid.UUID
    segment_id: uuid.UUID | None
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
    archived_at: datetime | None = None


class CampaignActivityResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_type: str
    created_at: datetime


class CampaignDetailResponse(BaseModel):
    """A campaign with the template, pending work and recorded activity."""

    campaign: CampaignResponse
    template: WhatsAppTemplateResponse
    pending_recipients: int
    activity: list[CampaignActivityResponse]
    # Read live from the recipient rows on every request, so the dashboard never shows
    # the campaign's denormalized counters from before Meta's later reports arrived.
    status_counts: dict[RecipientStatus, int] = Field(default_factory=dict)
    failure_reasons: list[CampaignFailureReason] = Field(default_factory=list)


class CampaignFailureReason(BaseModel):
    """Failed recipients sharing one Meta error."""

    error_code: str | None
    error_message: str | None
    count: int


class CampaignRecipientResponse(BaseModel):
    """One frozen recipient (§7 snapshot)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    lead_id: uuid.UUID
    phone_e164: str
    status: RecipientStatus
    attempts: int
    provider_message_id: str | None
    error_code: str | None = None
    error_message: str | None
    # Who the number belongs to, from the lead. Absent when the lead has no name.
    full_name: str | None = None
    queued_at: datetime | None = None
    sent_at: datetime | None = None
    delivered_at: datetime | None = None
    read_at: datetime | None = None
    failed_at: datetime | None = None


class CampaignRecipientPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[CampaignRecipientResponse]


class LeadCampaignResponse(BaseModel):
    """A campaign recipient record attached to a lead."""

    campaign_id: uuid.UUID
    campaign_name: str
    campaign_created_at: datetime
    recipient_status: RecipientStatus
    sent_at: datetime | None


class SendCampaignResponse(BaseModel):
    """The result of confirming Send Broadcast.

    `queued` counts tasks handed to the queue. Delivery happens asynchronously in the
    worker; nothing is sent inside this request (§4).
    """

    campaign: CampaignResponse
    queued: int
    skipped_as_suppressed: int


# ---------------------------------------------------------------- knowledge (§11)


class CreateKnowledgeBaseRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    # Optional: NULL means shared across the product's segments ([D56]).
    segment_id: uuid.UUID | None = None

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, value: str) -> str:
        """A whitespace-only name passes min_length but is unusable: it is invisible
        in the dashboard list and unquotable in an AI tool call."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must not be blank")
        return stripped


class KnowledgeBaseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    product_id: uuid.UUID
    segment_id: uuid.UUID | None
    name: str
    description: str | None
    created_at: datetime


class KnowledgeDocumentResponse(BaseModel):
    """A document and its ingestion state.

    `status` and `error_message` are what §17 requires the dashboard to surface: a
    document is only retrievable once READY, and a failed ingestion must say why.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    knowledge_base_id: uuid.UUID
    title: str
    source_filename: str | None
    content_type: str | None
    byte_size: int | None
    status: DocumentStatus
    chunk_count: int
    error_message: str | None
    created_at: datetime
    archived_at: datetime | None


# ---------------------------------------------------------------- leads (§10, §16)


class LeadResponse(BaseModel):
    """A lead as the dashboard sees it.

    `qualification_data` is included read-only: §17 wants the AI's extracted facts
    visible, but [D18] makes it the AI's target, not something a human edits here.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    product_id: uuid.UUID
    phone_e164: str
    full_name: str | None
    email: str | None
    company: str | None
    designation: str | None
    city: str | None
    status: LeadStatus
    qualification_data: dict[str, Any]
    opted_out: bool
    source_audience_id: uuid.UUID | None
    last_contacted_at: datetime | None
    created_at: datetime
    archived_at: datetime | None = None


class LeadPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[LeadResponse]


class UpdateLeadRequest(BaseModel):
    """Human corrections to a lead.

    Notably absent: `qualification_data` and `opted_out`. The first belongs to the AI
    ([D18]); the second is consent, which is not an editable attribute — it changes
    only through the opt-out flow.

    `status` is present but validated against the §10 transition table, so a human can
    correct a misjudged lead without being able to invent a transition the
    specification does not allow.
    """

    full_name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=320)
    company: str | None = Field(default=None, max_length=255)
    designation: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=255)
    status: LeadStatus | None = None


# ---------------------------------------------------------------- conversations (§9, §10)


class ConversationTurnResponse(BaseModel):
    """One turn of the transcript.

    `tool_calls` records what the AI asked for. In Phase 9 nothing is executed, and
    the stored shape says so explicitly rather than leaving it ambiguous.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: str
    content: str
    message_id: uuid.UUID | None
    tool_calls: dict[str, Any] | None
    created_at: datetime


class ConversationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    # Snapshotted at creation and never re-derived (§28.5).
    product_id: uuid.UUID
    segment_id: uuid.UUID | None
    lead_id: uuid.UUID
    status: ConversationStatus
    last_inbound_at: datetime | None
    last_outbound_at: datetime | None
    closed_at: datetime | None
    ai_paused_until: datetime | None = None
    created_at: datetime


class TeamReplyRequest(BaseModel):
    """A reply typed by a team member in the dashboard."""

    body: str = Field(min_length=1, max_length=4096)
    # Hours the assistant stays quiet after this reply, so it does not answer over
    # the person. 0 sends the reply and leaves the assistant running.
    pause_assistant_hours: int = Field(default=12, ge=0, le=72)


class TeamReplyAccepted(BaseModel):
    queued: bool
    ai_paused_until: datetime | None


class TemplateMessageRequest(BaseModel):
    """An approved template another system asks the platform to send.

    `idempotency_key` is the caller's id for this message: the same key sends once,
    however often the caller retries.
    """

    phone: str = Field(min_length=6, max_length=32)
    full_name: str | None = Field(default=None, max_length=255)
    template_name: str = Field(min_length=1, max_length=512)
    language: str = Field(default="en", min_length=2, max_length=20)
    variables: list[str] = Field(default_factory=list, max_length=10)
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("variables")
    @classmethod
    def _variables_are_sendable(cls, value: list[str]) -> list[str]:
        # Meta refuses an empty parameter, a newline or tab in one, and more than four
        # spaces in a row; failing here tells the caller which, instead of a send error.
        for item in value:
            if not item.strip() or len(item) > 1024:
                raise ValueError("each variable must be 1-1024 characters")
            if any(ch in item for ch in "\n\t") or "     " in item:
                raise ValueError("variables cannot contain newlines, tabs or 5+ spaces")
        return value


class TemplateMessageAccepted(BaseModel):
    queued: bool


class ConversationDetailResponse(BaseModel):
    """A conversation with its transcript and whether a free-form reply is possible.

    `within_service_window` is computed rather than stored: it is a function of
    `last_inbound_at` and the clock, and a stored copy would be wrong seconds later.
    The dashboard needs it to explain why a reply cannot be sent (§8).
    """

    conversation: ConversationResponse
    lead: LeadResponse
    within_service_window: bool
    total_turns: int
    turns: list[ConversationTurnResponse]


class ConversationListItem(ConversationResponse):
    """A conversation as the inbox lists it: who it is with and the latest message."""

    lead_name: str | None = None
    lead_phone: str | None = None
    last_message: str | None = None
    last_message_direction: MessageDirection | None = None
    last_message_at: datetime | None = None
    # The customer wrote last and nobody has answered yet.
    awaiting_reply: bool = False


class ConversationPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[ConversationListItem]


# ---------------------------------------------------------------- bookings (§12, §16)


class BookingResponse(BaseModel):
    """A demo booking.

    `timezone` is per booking rather than read from configuration: a policy change
    must not retroactively reinterpret a booking already made ([D8]).
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    product_id: uuid.UUID
    lead_id: uuid.UUID
    conversation_id: uuid.UUID | None
    calendar_event_id: str | None
    start_time: datetime
    end_time: datetime
    timezone: str
    status: BookingStatus
    meeting_link: str | None
    customer_email: str | None
    failure_reason: str | None
    confirmed_at: datetime | None
    cancelled_at: datetime | None
    created_at: datetime


class BookingPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[BookingResponse]


# ---------------------------------------------------------------- notifications (§13)


class NotificationResponse(BaseModel):
    """One notification, naming its product ([D47]).

    `product` is NULL only for a notification that is about no single product.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    product_id: uuid.UUID | None
    type: NotificationType
    title: str
    message: str
    related_entity_type: str | None
    related_entity_id: uuid.UUID | None
    is_read: bool
    read_at: datetime | None
    created_at: datetime
    product: ProductRef | None = None


class NotificationPage(BaseModel):
    """A page of notifications.

    `unread` counts every unread notification, not just those on this page: the bell
    badge shows it regardless of what is being viewed.
    """

    total: int
    unread: int
    limit: int
    offset: int
    items: list[NotificationResponse]


class MarkAllReadResponse(BaseModel):
    marked_read: int


# ---------------------------------------------------------------- analytics (§17)


class OverviewResponse(BaseModel):
    """The nine §17 overview metrics, plus the totals that give them context.

    Delivery counts are cumulative: a delivered message was also sent, so `sent` does
    not fall as receipts arrive. `replies` counts *people*, not messages — a customer
    who sends five is one reply to a salesperson reading this.
    """

    campaigns: int
    campaigns_sending: int
    messages_sent: int
    delivered: int
    read: int
    failed: int
    replies: int
    total_leads: int
    interested: int
    qualified: int
    demos_booked: int
    # Scoped to the caller: notifications are per-user ([D3]).
    unread_notifications: int


class LeadFunnelResponse(BaseModel):
    """Lead counts per state, every state present."""

    counts: dict[str, int]
