"""Error taxonomy shared by both services.

Specification §20 requires distinguishing retryable from non-retryable failures so
background jobs retry only what is worth retrying. Provider implementations raise
these; the worker (Phase 7) decides retry behaviour from the classification rather
than from provider-specific exception types.
"""

from __future__ import annotations


class AppError(Exception):
    """Base class for all deliberate application errors."""


class ConfigurationError(AppError):
    """Configuration is missing or inconsistent."""


class ProviderError(AppError):
    """An external provider call failed.

    `retryable` carries the §20 classification. `provider` names the integration for
    log correlation without leaking credentials.
    """

    retryable: bool = False

    def __init__(self, message: str, *, provider: str | None = None) -> None:
        super().__init__(message)
        self.provider = provider

    def __str__(self) -> str:
        base = super().__str__()
        return f"[{self.provider}] {base}" if self.provider else base


class RetryableProviderError(ProviderError):
    """Timeout, network failure, rate limit or provider outage (§20)."""

    retryable = True


class NonRetryableProviderError(ProviderError):
    """Invalid response, auth failure or rejected request (§20)."""

    retryable = False


class ProviderNotImplementedError(ConfigurationError):
    """A provider is selected by configuration but not implemented in this phase.

    Phase 1 ships the abstractions and fakes; each real integration arrives in its
    own phase. Selecting one early fails loudly instead of silently doing nothing.
    """

    def __init__(self, provider: str, phase: str) -> None:
        super().__init__(
            f"Provider '{provider}' is not implemented yet; it is scheduled for {phase}. "
            f"Use the fake provider until then."
        )
        self.provider = provider
        self.phase = phase
