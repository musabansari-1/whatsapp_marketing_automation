"""Rate limit enforcement (§19).

Sits between the routes and the counter. The routes say *what* is being limited, the
domain says *whether* it is allowed, and this turns a refusal into the HTTP response.

**Fails closed.** If the counter cannot be reached, the request is refused. The
alternative — letting requests through when the limiter is broken — means the protection
disappears at exactly the moment something is wrong, which is when it is most likely to
be needed. The cost is that a database problem takes logins down; but a database problem
already takes logins down, because authentication reads from it.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.domain.rate_limit import (
    RateLimitRule,
    RateLimitScope,
    account_identifier,
    client_identifier,
)
from app.persistence.repositories import rate_limits as rate_limit_repo

logger = get_logger(__name__)


def rule_for(scope: RateLimitScope, settings: Settings) -> RateLimitRule:
    """The configured rule for a scope.

    Every number is configuration: §19 asks for appropriate limits without saying what
    they are, and picking them in code would make them a deployment problem to change.
    """
    match scope:
        case RateLimitScope.LOGIN_IP:
            return RateLimitRule(
                limit=settings.rate_limit_login_ip_attempts,
                window_seconds=settings.rate_limit_login_window_seconds,
            )
        case RateLimitScope.LOGIN_ACCOUNT:
            return RateLimitRule(
                limit=settings.rate_limit_login_account_attempts,
                window_seconds=settings.rate_limit_login_window_seconds,
            )
        case RateLimitScope.WEBHOOK_IP:
            return RateLimitRule(
                limit=settings.rate_limit_webhook_requests,
                window_seconds=settings.rate_limit_webhook_window_seconds,
            )
        case RateLimitScope.UPLOAD_USER:
            return RateLimitRule(
                limit=settings.rate_limit_upload_requests,
                window_seconds=settings.rate_limit_upload_window_seconds,
            )


def caller_address(request: Request) -> str:
    """The caller's address, as well as it can be known behind a proxy."""
    return client_identifier(
        request.headers.get("x-forwarded-for"),
        request.client.host if request.client else None,
    )


async def enforce(
    session: AsyncSession,
    settings: Settings,
    *,
    scope: RateLimitScope,
    identifier: str,
) -> None:
    """Count this attempt and raise 429 if it is over the limit.

    Raises rather than returning a decision, because every caller would otherwise have
    to remember to check — and the one that forgot would be the one that mattered.
    """
    if not settings.rate_limit_enabled:
        return

    rule = rule_for(scope, settings)

    try:
        decision = await rate_limit_repo.hit(session, scope=scope, identifier=identifier, rule=rule)
    except Exception as exc:  # noqa: BLE001 - fail closed, see the module docstring
        logger.error("rate_limit_unavailable", scope=scope.value, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service temporarily unavailable",
        ) from exc

    if decision.allowed:
        return

    # The identifier is not logged for the account scope: it is an email address, and
    # §19 asks to avoid unnecessary PII in logs. The scope and the count say enough to
    # diagnose it.
    logger.warning(
        "rate_limit_exceeded",
        scope=scope.value,
        limit=decision.limit,
        attempts=decision.current,
    )

    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many requests. Try again shortly.",
        headers={"Retry-After": str(decision.retry_after_seconds)},
    )


async def enforce_login(
    session: AsyncSession, settings: Settings, *, request: Request, email: str
) -> None:
    """Both login limits, per-IP first.

    Two scopes because neither is sufficient alone. Per-IP catches broad scanning but is
    defeated by a distributed attempt and can lock out an office behind one NAT address.
    Per-account protects a known user but lets an attacker lock that user out. Together
    they cover each other's gap.

    Per-IP is checked first so that a distributed attack on one account still consumes
    its own addresses' budgets rather than only the account's.
    """
    await enforce(
        session,
        settings,
        scope=RateLimitScope.LOGIN_IP,
        identifier=caller_address(request),
    )
    await enforce(
        session,
        settings,
        scope=RateLimitScope.LOGIN_ACCOUNT,
        identifier=account_identifier(email),
    )


__all__ = ["caller_address", "enforce", "enforce_login", "rule_for"]
