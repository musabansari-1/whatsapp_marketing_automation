"""Inbound conversation processing (specification §9, §10, §18, §20, [D36]).

This is the seam the AI plugs into. Phase 9 builds the whole flow — history, states,
window enforcement, idempotency, reply delivery — around an `LLMProvider` that is
still the fake. Phase 10 swaps the provider and adds retrieval, tools and extraction;
nothing in this module's shape has to change for that.

What Phase 9 deliberately does *not* do:

- decide qualification. [D9]/[D37] put the criteria in Phase 10, and inventing them
  here is exactly what that decision forbids.
- let the model choose the conversation's state. §10 says the AI cannot invent
  transitions, so the state moves through the domain table below and nowhere else.
- call any tool. The authorized six arrive in Phase 10; a tool call from the fake is
  recorded and ignored rather than dispatched.

The ordering in `process_conversation_turn` is deliberate and each step prevents a
specific failure:

1. load the conversation and the triggering message
2. already replied under this idempotency key? — a redelivery must not reply twice
3. conversation CLOSED? — [D36] makes it terminal; nothing is sent
4. suppressed? — [D16], because the customer may have opted out since the task was
   queued
5. outside the 24-hour window? — §8 forbids the free-form reply; recorded, not sent
6. record the customer's turn, once
7. ask the model
8. send, then record the reply and move the state
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import tools as ai_tools
from app.ai.agent import AgentOutcome, respond_to_customer
from app.core.config import Settings, get_settings
from app.core.correlation import bind_log_context
from app.core.errors import ProviderError
from app.core.logging import get_logger
from app.domain.conversation import LeadStateError, can_transition_conversation
from app.domain.enums import ConversationStatus, LeadStatus, MessageStatus
from app.domain.qualification import rules_from_settings
from app.domain.rag import RetrievalPolicy, RetrievalResult, retrieve
from app.domain.retry import RetryPolicy, backoff_seconds, classify, should_retry
from app.domain.slots import policy_from_settings
from app.domain.whatsapp_webhook import is_within_service_window
from app.integrations.llm import Message as LLMMessage
from app.integrations.llm import Role
from app.integrations.queue import TaskRequest
from app.integrations.registry import (
    build_calendar_provider,
    build_embedding_provider,
    build_llm_provider,
    build_whatsapp_provider,
)
from app.integrations.whatsapp import OutboundTextMessage
from app.persistence.database import get_session_factory
from app.persistence.models import AIRun, Conversation, Lead, WhatsAppSuppression
from app.persistence.repositories import conversations as conversation_repo
from app.persistence.repositories import knowledge as knowledge_repo
from app.worker.handlers.campaign_send import RetryableSendError

logger = get_logger(__name__)

# How many prior turns are given to the model. Configuration in all but name — it is
# read from settings; this is only the fallback when unset.
DEFAULT_HISTORY_TURNS = 20


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    """What happened, for logging and for the task response."""

    result: str
    conversation_id: str
    replied: bool = False

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "result": self.result,
            "conversation_id": self.conversation_id,
            "replied": self.replied,
        }


def reply_idempotency_key(message_id: str) -> str:
    """One reply per inbound message, by construction.

    Derived from the triggering message rather than generated, so a retried task
    computes the same key and the unique constraint on `messages.idempotency_key`
    refuses the second write.
    """
    return f"reply-{message_id}"


async def process_conversation_turn(task: TaskRequest) -> dict[str, str | bool]:
    """Handle one inbound customer message end to end."""
    settings = get_settings()

    message_id = task.payload.get("message_id")
    if not message_id:
        raise ValueError("message_id missing from task payload")

    factory = get_session_factory()

    async with factory() as session:
        message = await conversation_repo.get_message(session, uuid.UUID(str(message_id)))
        if message is None:
            # Nothing to retry against.
            raise ValueError(f"Message {message_id} not found")

        if message.conversation_id is None:
            raise ValueError(f"Message {message_id} is not part of a conversation")

        conversation = await conversation_repo.get_conversation(session, message.conversation_id)
        lead = await session.get(Lead, message.lead_id) if message.lead_id else None
        if conversation is None or lead is None:
            raise ValueError(f"Conversation or lead missing for message {message_id}")

        bind_log_context(
            conversation_id=str(conversation.id),
            lead_id=str(lead.id),
        )

        idempotency_key = reply_idempotency_key(str(message.id))

        # A reply already exists for this message: a redelivery, or a worker that sent
        # and then crashed before finishing. Either way, replying again would send the
        # customer a duplicate.
        if await conversation_repo.find_reply_by_idempotency_key(session, idempotency_key):
            logger.info("conversation_turn_already_handled", message_id=str(message.id))
            return TurnOutcome("already_replied", str(conversation.id)).as_dict()

        # [D36]: a closed conversation is never reopened. A message arriving against a
        # closed thread is the webhook's problem to route into a new conversation, not
        # this handler's to resurrect.
        if conversation.status is ConversationStatus.CLOSED:
            logger.info("conversation_turn_skipped", reason="closed")
            return TurnOutcome("conversation_closed", str(conversation.id)).as_dict()

        # [D16]: consent may have been withdrawn after this task was queued. The
        # suppression table is authoritative; `lead.opted_out` alone is never enough.
        if await _is_suppressed(session, lead.phone_e164):
            logger.info("conversation_turn_skipped", reason="suppressed")
            return TurnOutcome("suppressed", str(conversation.id)).as_dict()

        # §8: outside the 24-hour window Meta permits only an approved template, and
        # [D24] leaves that template's content with the client. The turn is recorded
        # so the transcript stays complete and the dashboard shows what was missed.
        if not is_within_service_window(
            conversation.last_inbound_at,
            window_hours=settings.customer_service_window_hours,
        ):
            await conversation_repo.record_turn_once(
                session,
                conversation=conversation,
                role=conversation_repo.ROLE_USER,
                content=message.body or "",
                message_id=message.id,
            )
            await session.commit()
            logger.warning("conversation_reply_blocked", reason="outside_service_window")
            return TurnOutcome("outside_service_window", str(conversation.id)).as_dict()

        _turn, created = await conversation_repo.record_turn_once(
            session,
            conversation=conversation,
            role=conversation_repo.ROLE_USER,
            content=message.body or "",
            message_id=message.id,
        )
        if created:
            # A customer who replies is no longer merely CONTACTED. Guarded rather
            # than assumed: the lead may already be further along, and §10 does not
            # allow walking a state backwards.
            await _advance_to_responded(session, lead)

        history = await conversation_repo.get_history(
            session,
            conversation.id,
            limit=settings.conversation_history_turns or DEFAULT_HISTORY_TURNS,
        )
        prompt = _history_messages(history)
        conversation_id = conversation.id
        lead_id = lead.id
        lead_phone = lead.phone_e164
        question = message.body or ""
        await session.commit()

    started = datetime.now(UTC)
    provider = build_llm_provider(settings)

    # Retrieval happens before the model runs and outside the tool loop: §11 makes it
    # the evidence the reply is grounded in, not something the model chooses to
    # consult. A failure here is not fatal — the agent is told there is no evidence and
    # uses the approved fallback rather than losing the turn.
    retrieval = await _retrieve(settings, question)

    try:
        async with factory() as session:
            lead = await session.get(Lead, lead_id)
            conversation = await conversation_repo.get_conversation(session, conversation_id)
            if lead is None or conversation is None:
                raise ValueError(f"Conversation {conversation_id} disappeared mid-turn")

            tool_context = ai_tools.ToolContext(
                session=session,
                lead=lead,
                conversation=conversation,
                slot_policy=policy_from_settings(settings),
                calendar=build_calendar_provider(settings),
                qualification_rules=rules_from_settings(settings.qualification_required_fields),
            )

            outcome = await respond_to_customer(
                provider=provider,
                system_prompt=settings.ai_system_prompt,
                history=prompt,
                retrieval=retrieval,
                context=tool_context,
                fallback_message=settings.msg_rag_fallback,
                max_rounds=settings.ai_max_tool_rounds,
            )
            # Tools write through this session, so their effects are committed with
            # the turn rather than left dangling if the send fails.
            await session.commit()
    except ProviderError as exc:
        await _record_failed_run(conversation_id, provider.model, str(exc))
        _raise_if_retryable(settings, exc)
        logger.warning("conversation_llm_failed", error=str(exc))
        return TurnOutcome("llm_failed", str(conversation_id)).as_dict()

    latency_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    reply_text = outcome.reply.strip()

    if not reply_text:
        # The agent substitutes the approved fallback for an empty completion, so
        # reaching here means even that was blank — a misconfiguration worth seeing.
        await _record_outcome_run(conversation_id, outcome, latency_ms=latency_ms)
        logger.warning("conversation_empty_completion")
        return TurnOutcome("empty_completion", str(conversation_id)).as_dict()

    idempotency_key = reply_idempotency_key(str(message_id))

    try:
        result = await build_whatsapp_provider(settings).send_text_message(
            OutboundTextMessage(
                to_e164=lead_phone,
                body=reply_text,
                idempotency_key=idempotency_key,
            )
        )
    except ProviderError as exc:
        await _record_failed_run(conversation_id, provider.model, str(exc))
        _raise_if_retryable(settings, exc)
        logger.warning("conversation_reply_send_failed", error=str(exc))
        return TurnOutcome("send_failed", str(conversation_id)).as_dict()

    async with factory() as session:
        conversation = await conversation_repo.get_conversation(session, conversation_id)
        if conversation is None:
            raise ValueError(f"Conversation {conversation_id} disappeared mid-turn")
        lead = await session.get(Lead, conversation.lead_id)
        if lead is None:
            raise ValueError(f"Lead for conversation {conversation_id} disappeared mid-turn")

        sent_message = await conversation_repo.record_outbound_reply(
            session,
            lead=lead,
            conversation=conversation,
            body=reply_text,
            idempotency_key=idempotency_key,
            provider_message_id=result.provider_message_id,
            status=MessageStatus.SENT,
            sent_at=datetime.now(UTC),
        )
        await conversation_repo.record_turn(
            session,
            conversation=conversation,
            role=conversation_repo.ROLE_ASSISTANT,
            content=reply_text,
            message_id=sent_message.id,
            tool_calls={"executed": outcome.tool_calls} if outcome.tool_calls else None,
        )
        await conversation_repo.touch_outbound(session, conversation)

        # The AI extracted; the backend decides. `proposed_status` came from the
        # qualification rules reading the facts a tool recorded — never from the model
        # asserting a state ([D9], [D18]) — and it still goes through the §10 table.
        if outcome.proposed_status is not None and outcome.proposed_status is not lead.status:
            try:
                await conversation_repo.transition_lead(session, lead, outcome.proposed_status)
                logger.info(
                    "lead_status_advanced_by_rules",
                    new_status=outcome.proposed_status.value,
                )
            except LeadStateError as exc:
                # The rules only ever propose permitted transitions, so this means the
                # lead moved underneath us. Logged, not fatal: the reply is already
                # sent and the next turn will re-evaluate.
                logger.warning("lead_transition_rejected", error=str(exc))

        await _apply_conversation_state(session, conversation, outcome)

        session.add(
            _build_run(
                conversation_id=conversation.id,
                lead_id=lead.id,
                outcome=outcome,
                latency_ms=latency_ms,
            )
        )
        await session.commit()

    logger.info("conversation_turn_completed", latency_ms=latency_ms)
    return TurnOutcome("replied", str(conversation_id), replied=True).as_dict()


# ---------------------------------------------------------------- internals


def _history_messages(history: list[Any]) -> list[LLMMessage]:
    """Turn the stored transcript into the provider's message shape.

    Only the conversation itself. The system prompt and the retrieved knowledge are
    assembled by the agent, which is where §11's rule about document text never
    overriding instructions is applied.
    """
    messages: list[LLMMessage] = []

    for turn in history:
        try:
            role = Role(turn.role)
        except ValueError:
            # An unknown role would be rejected by the provider. Treating it as a
            # system note keeps one bad row from breaking the whole conversation.
            role = Role.SYSTEM
        messages.append(LLMMessage(role=role, content=turn.content))

    return messages


async def _retrieve(settings: Settings, question: str) -> RetrievalResult | None:
    """Find knowledge relevant to what the customer asked (§11).

    Returns None when retrieval is off or unavailable, which the agent reads the same
    way as "nothing relevant": it tells the model to use the approved fallback rather
    than answer unsupported. A retrieval failure must not lose the customer's turn —
    the platform can still reply from the conversation itself.
    """
    if not settings.ai_retrieval_enabled or not question.strip():
        return None

    try:
        embedding = build_embedding_provider(settings)
        vector = await embedding.embed_query(question)

        factory = get_session_factory()
        async with factory() as session:
            knowledge_base = await _knowledge_base_id(session, settings)
            if knowledge_base is None:
                return None

            candidates = await knowledge_repo.search(
                session,
                knowledge_base_id=knowledge_base,
                query_vector=vector,
                limit=settings.rag_top_k,
                # [D1]: comparing vectors from different models is meaningless, so a
                # search after a model change returns nothing rather than nonsense.
                embedding_model=embedding.model_id,
            )
    except Exception as exc:  # noqa: BLE001 - retrieval is best-effort by design
        logger.warning("conversation_retrieval_failed", error=str(exc))
        return None

    return retrieve(
        candidates,
        policy=RetrievalPolicy(
            top_k=settings.rag_top_k,
            min_similarity=settings.rag_min_similarity,
            max_context_chars=settings.rag_max_context_chars,
        ),
    )


async def _knowledge_base_id(session: AsyncSession, settings: Settings) -> uuid.UUID | None:
    """Which knowledge base the AI answers from.

    Named in configuration when there are several. With none configured it uses the
    only one, and declines to guess when several exist — answering from an arbitrary
    knowledge base is worse than not answering.
    """
    bases = await knowledge_repo.list_knowledge_bases(session)
    if not bases:
        return None

    if settings.ai_knowledge_base_name:
        wanted = settings.ai_knowledge_base_name.casefold()
        for base in bases:
            if base.name.casefold() == wanted:
                return base.id
        logger.warning("ai_knowledge_base_not_found", configured=settings.ai_knowledge_base_name)
        return None

    if len(bases) > 1:
        logger.warning("ai_knowledge_base_ambiguous", count=len(bases))
        return None

    return bases[0].id


async def _is_suppressed(session: AsyncSession, phone_e164: str) -> bool:
    result = await session.execute(
        select(WhatsAppSuppression.id).where(WhatsAppSuppression.phone_e164 == phone_e164)
    )
    return result.scalars().first() is not None


async def _advance_to_responded(session: AsyncSession, lead: Lead) -> None:
    """Record that the customer replied, if that is a step forward.

    Only ever moves NEW/CONTACTED to RESPONDED. A lead that is already QUALIFYING has
    obviously responded, and §10 permits no edge back.
    """
    if lead.status in {LeadStatus.NEW, LeadStatus.CONTACTED}:
        await conversation_repo.transition_lead(session, lead, LeadStatus.RESPONDED)


async def _apply_conversation_state(
    session: AsyncSession, conversation: Conversation, outcome: AgentOutcome
) -> None:
    """Move the conversation according to what the turn actually did.

    A confirmed booking moves it to BOOKING — and only a confirmed one:
    `booked_event_id` is set by the tool after Calendar returned an event, never
    because the model said it booked something.
    """
    if outcome.booked_event_id and can_transition_conversation(
        conversation.status, ConversationStatus.BOOKING
    ):
        await conversation_repo.transition_conversation(
            session, conversation, ConversationStatus.BOOKING
        )
        return

    # We have spoken; it is the customer's turn. Only from ACTIVE: the table also
    # permits BOOKING → WAITING_FOR_USER, but a booking conversation is waiting on a
    # slot choice rather than on ordinary conversation, and Phase 11 owns when that
    # ends.
    if conversation.status is ConversationStatus.ACTIVE:
        await conversation_repo.transition_conversation(
            session, conversation, ConversationStatus.WAITING_FOR_USER
        )


def _run_tool_calls(outcome: AgentOutcome) -> dict[str, Any] | None:
    """The tool record for `ai_runs`.

    Only what actually ran, with its result. §21 needs to be able to explain why a
    customer was told what they were told, and a request that failed is as much a part
    of that as one that succeeded.
    """
    if not outcome.tool_calls:
        return None
    return {"executed": outcome.tool_calls}


def _build_run(
    *,
    conversation_id: uuid.UUID,
    lead_id: uuid.UUID,
    outcome: AgentOutcome,
    latency_ms: int,
) -> AIRun:
    from app.domain.enums import AIRunStatus

    return AIRun(
        conversation_id=conversation_id,
        lead_id=lead_id,
        # Recorded per run rather than assumed: [D17] leaves the model unchosen until
        # benchmarking, OpenRouter can fall back to another provider, and it may
        # therefore differ between runs.
        model=outcome.model,
        status=AIRunStatus.SUCCESS,
        prompt_tokens=outcome.prompt_tokens,
        completion_tokens=outcome.completion_tokens,
        latency_ms=latency_ms,
        tool_calls=_run_tool_calls(outcome),
        # Which chunks grounded the answer (§21). Without this, a wrong answer cannot
        # be traced back to the document that caused it.
        retrieved_chunk_ids=(
            {"chunk_ids": outcome.retrieved_chunk_ids, "used_fallback": outcome.used_fallback}
            if outcome.retrieved_chunk_ids or outcome.used_fallback
            else None
        ),
    )


async def _record_outcome_run(
    conversation_id: uuid.UUID, outcome: AgentOutcome, *, latency_ms: int
) -> None:
    """Record a run that produced nothing to send."""
    factory = get_session_factory()
    async with factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            return
        session.add(
            _build_run(
                conversation_id=conversation_id,
                lead_id=conversation.lead_id,
                outcome=outcome,
                latency_ms=latency_ms,
            )
        )
        await session.commit()


async def _record_failed_run(conversation_id: uuid.UUID, model: str, reason: str) -> None:
    """Record a failed run so §21 has the trail even when nothing was sent."""
    from app.domain.enums import AIRunStatus

    factory = get_session_factory()
    async with factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        session.add(
            AIRun(
                conversation_id=conversation_id,
                lead_id=conversation.lead_id if conversation else None,
                model=model,
                status=AIRunStatus.FAILED,
                error_message=reason[:1000],
            )
        )
        await session.commit()


def _raise_if_retryable(settings: Settings, exc: ProviderError) -> None:
    """Re-raise as a retryable failure when another attempt could succeed.

    Attempt counting lives with the transport: Cloud Tasks knows how many times it has
    delivered this task, so the policy here only decides whether the failure class is
    worth retrying at all.
    """
    policy = RetryPolicy(
        max_attempts=settings.send_max_attempts,
        base_seconds=settings.send_backoff_base_seconds,
        max_seconds=settings.send_backoff_max_seconds,
        jitter=settings.send_backoff_jitter,
    )
    failure = classify(exc)
    if should_retry(failure=failure, attempts_made=1, policy=policy):
        raise RetryableSendError(
            str(exc),
            retry_after_seconds=backoff_seconds(attempts_made=1, policy=policy),
        ) from exc


__all__ = ["process_conversation_turn", "reply_idempotency_key"]
