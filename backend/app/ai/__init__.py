"""The AI agent (specification §9, §11, §12, [D9], [D10], [D18]).

Kept apart from `app/worker/handlers/` because this is not a task: it is the reasoning
layer a task calls. The worker owns delivery, retries and idempotency; this owns the
conversation with the model.

Two boundaries are enforced here rather than trusted to a prompt:

- `tools.py` is the only route from the model to the backend (§9).
- The model never sets a state. It extracts and proposes; `app/domain/` decides.

`reply_guard.py` is the third: what the customer receives is the model's prose, with any
tool call it printed along the way removed.
"""

from app.ai.agent import AgentOutcome, respond_to_customer
from app.ai.reply_guard import clean_customer_reply
from app.ai.tools import ToolContext, ToolResult, dispatch, tool_specs

__all__ = [
    "AgentOutcome",
    "ToolContext",
    "ToolResult",
    "clean_customer_reply",
    "dispatch",
    "respond_to_customer",
    "tool_specs",
]
