"""The AI agent (specification §9, §11, §12, [D9], [D10], [D18]).

Kept apart from `app/worker/handlers/` because this is not a task: it is the reasoning
layer a task calls. The worker owns delivery, retries and idempotency; this owns the
conversation with the model.

Two boundaries are enforced here rather than trusted to a prompt:

- `tools.py` is the only route from the model to the backend (§9).
- The model never sets a state. It extracts and proposes; `app/domain/` decides.
"""

from app.ai.agent import AgentOutcome, respond_to_customer
from app.ai.tools import ToolContext, ToolResult, dispatch, tool_specs

__all__ = [
    "AgentOutcome",
    "ToolContext",
    "ToolResult",
    "dispatch",
    "respond_to_customer",
    "tool_specs",
]
