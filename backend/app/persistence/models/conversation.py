"""Conversations, messages and inbound webhook events (§8, §9, §10)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.enums import (
    AIRunStatus,
    ConversationStatus,
    MessageDirection,
    MessageStatus,
    WebhookEventStatus,
)
from app.persistence.base import Base
from app.persistence.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.persistence.models.lead import Lead


class Conversation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """An ongoing WhatsApp conversation with one lead (§10)."""

    __tablename__ = "conversations"

    # §28 product scope, NOT NULL since Phase 23. Snapshotted at creation from the
    # lead, which inbound routing now always resolves (§28.5, [D40]).
    product_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    # Nullable by design, permanently: §28 allows a product-wide entity, and a
    # product may have no segments at all. Never filled with a placeholder.
    segment_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("segments.id", ondelete="RESTRICT")
    )

    lead_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[ConversationStatus] = mapped_column(
        Enum(ConversationStatus, name="conversation_status", native_enum=True),
        nullable=False,
        default=ConversationStatus.ACTIVE,
    )

    # Meta only permits free-form replies within 24 hours of the last inbound
    # message. Phase 6 enforces that window; recording the timestamp here is what
    # makes the check possible.
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_outbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when a team member replies from the dashboard: until then the assistant
    # records the customer's turns but does not answer, so it never talks over a
    # person. NULL, or a time in the past, means the assistant answers.
    ai_paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lead: Mapped[Lead] = relationship(back_populates="conversations")
    messages: Mapped[list[ConversationMessage]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_conversations_lead_id", "lead_id"),
        Index("ix_conversations_status", "status"),
        Index("ix_conversations_product_id", "product_id"),
        Index("ix_conversations_segment_id", "segment_id"),
        # A segment must belong to this row's own product (§28.8). MATCH SIMPLE
        # skips the check when either column is NULL, so a product-wide row is
        # unaffected, and so is one with no product yet during the transition.
        ForeignKeyConstraint(
            ["product_id", "segment_id"],
            ["segments.product_id", "segments.id"],
            name="fk_conversations_segment_within_product",
            ondelete="RESTRICT",
        ),
    )


class Message(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Transport-level WhatsApp message record (§8).

    Distinct from `conversation_messages`: this table tracks delivery state against
    the provider, whereas the other is the AI's conversation history. A broadcast
    send produces a row here without any conversation existing yet.

    `provider_message_id` is unique — §15 requires unique constraints on provider IDs,
    and it is what makes status-webhook processing idempotent.
    """

    __tablename__ = "messages"

    # §28 product scope, NOT NULL since Phase 23. Inherited from the campaign or the
    # conversation — never inferred from the message or the sender's number.
    product_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )

    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("leads.id", ondelete="SET NULL")
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="SET NULL")
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL")
    )

    direction: Mapped[MessageDirection] = mapped_column(
        Enum(MessageDirection, name="message_direction", native_enum=True), nullable=False
    )
    status: Mapped[MessageStatus] = mapped_column(
        Enum(MessageStatus, name="message_status", native_enum=True),
        nullable=False,
        default=MessageStatus.QUEUED,
    )
    provider_message_id: Mapped[str | None] = mapped_column(String(255), unique=True)

    phone_e164: Mapped[str] = mapped_column(String(20), nullable=False)
    template_name: Mapped[str | None] = mapped_column(String(512))
    body: Mapped[str | None] = mapped_column(Text)

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)

    # Deduplicates a retried send task (§20).
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True)

    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_messages_lead_id", "lead_id"),
        Index("ix_messages_campaign_id", "campaign_id"),
        Index("ix_messages_conversation_id", "conversation_id"),
        Index("ix_messages_status", "status"),
        Index("ix_messages_phone_e164", "phone_e164"),
        Index("ix_messages_product_id", "product_id"),
    )


class ConversationMessage(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One turn of conversation history, as the AI sees it (§9).

    Kept separate from `messages` because the AI's view is a role/content transcript,
    while `messages` is delivery state. Retrieved RAG context is data, never
    instructions (§11), so it is stored alongside rather than merged into content.
    """

    __tablename__ = "conversation_messages"

    # Ordering cannot rely on `created_at`: it defaults to `now()`, which in PostgreSQL
    # is the *transaction* timestamp, so every turn written in one transaction carries
    # an identical value and their order becomes arbitrary. That order is exactly what
    # the AI reads history in, so a database-assigned monotonic sequence decides it.
    sequence: Mapped[int] = mapped_column(
        BigInteger, Identity(always=False), nullable=False, unique=True
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    # "user", "assistant", "system", "tool" — matching the LLM transport roles.
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Links back to the transport record when this turn was actually sent/received.
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL")
    )
    # Tool calls requested or results returned, for audit of the authorized six (§9).
    tool_calls: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")

    __table_args__ = (
        Index("ix_conversation_messages_conversation_created", "conversation_id", "created_at"),
        # The index the transcript is actually read by.
        Index("ix_conversation_messages_conversation_sequence", "conversation_id", "sequence"),
    )


class WebhookEvent(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A raw inbound webhook delivery (§8).

    The webhook persists here and returns immediately; processing happens in the
    worker. `provider_event_id` is unique, which is the duplicate-event protection §8
    requires — Meta redelivers, and a repeat must be recognized rather than
    reprocessed.
    """

    __tablename__ = "webhook_events"

    provider_event_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    status: Mapped[WebhookEventStatus] = mapped_column(
        Enum(WebhookEventStatus, name="webhook_event_status", native_enum=True),
        nullable=False,
        default=WebhookEventStatus.RECEIVED,
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_webhook_events_status", "status"),
        Index("ix_webhook_events_created_at", "created_at"),
    )


class AIRun(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One LLM invocation, recorded for observability (§21).

    `model` is stored per run rather than assumed, because [D17] leaves the model
    unchosen until Phase 10 benchmarking and it may change between runs.
    """

    __tablename__ = "ai_runs"

    # §28 product scope, NOT NULL since Phase 23. Taken from the conversation, never
    # resolved independently during execution.
    product_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )

    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL")
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("leads.id", ondelete="SET NULL")
    )

    model: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[AIRunStatus] = mapped_column(
        Enum(AIRunStatus, name="ai_run_status", native_enum=True), nullable=False
    )

    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    # Which of the authorized six the model requested, and what RAG supplied.
    tool_calls: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    retrieved_chunk_ids: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    error_message: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_ai_runs_conversation_id", "conversation_id"),
        Index("ix_ai_runs_lead_id", "lead_id"),
        Index("ix_ai_runs_status", "status"),
        Index("ix_ai_runs_correlation_id", "correlation_id"),
        Index("ix_ai_runs_product_id", "product_id"),
    )
