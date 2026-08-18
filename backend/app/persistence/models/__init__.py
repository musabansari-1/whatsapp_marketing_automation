"""ORM models — the 19 tables of specification §15 plus [D16]'s suppression table.

Importing this package registers every model on `Base.metadata`, which is what
Alembic autogenerate reflects against. `app/persistence/base.py` imports it for that
reason.
"""

from app.persistence.models.audience import Audience, AudienceContact
from app.persistence.models.booking import Booking
from app.persistence.models.campaign import (
    META_STATUS_APPROVED,
    Campaign,
    CampaignRecipient,
    WhatsAppTemplate,
)
from app.persistence.models.conversation import (
    AIRun,
    Conversation,
    ConversationMessage,
    Message,
    WebhookEvent,
)
from app.persistence.models.knowledge import (
    EMBEDDING_DIMENSION,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIngestionJob,
)
from app.persistence.models.lead import Lead, WhatsAppSuppression
from app.persistence.models.notification import Notification
from app.persistence.models.session import Session
from app.persistence.models.user import User

__all__ = [
    "EMBEDDING_DIMENSION",
    "META_STATUS_APPROVED",
    "AIRun",
    "Audience",
    "AudienceContact",
    "Booking",
    "Campaign",
    "CampaignRecipient",
    "Conversation",
    "ConversationMessage",
    "KnowledgeBase",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "KnowledgeIngestionJob",
    "Lead",
    "Message",
    "Notification",
    "Session",
    "User",
    "WebhookEvent",
    "WhatsAppSuppression",
    "WhatsAppTemplate",
]
