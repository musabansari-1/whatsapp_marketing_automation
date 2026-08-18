"""Authentication endpoints (specification §16, [D2], [D13])."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.api.deps import CurrentUser, DbSession, SettingsDep
from app.api.schemas import LoginRequest, MessageResponse, UserResponse
from app.core.logging import get_logger
from app.core.security import (
    generate_session_token,
    hash_password,
    hash_session_token,
    needs_rehash,
    verify_dummy_password,
    verify_password,
)
from app.domain.auth import session_expiry
from app.persistence.repositories import sessions as session_repo
from app.persistence.repositories import users as user_repo

router = APIRouter(prefix="/auth", tags=["auth"])
logger = get_logger(__name__)

_INVALID_CREDENTIALS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid email or password",
)


def _set_session_cookie(response: Response, token: str, settings: SettingsDep) -> None:
    """Attach the session cookie.

    No `domain` attribute, deliberately: the BFF proxy forwards this Set-Cookie
    onward and the browser then scopes the cookie to the frontend origin, which is
    what keeps it same-site under [D13]. Setting a domain here would tie it to the
    Cloud Run host and break that.
    """
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        path="/",
    )


@router.post("/login", response_model=UserResponse)
async def login(
    payload: LoginRequest,
    response: Response,
    session: DbSession,
    settings: SettingsDep,
) -> UserResponse:
    """Exchange credentials for a session cookie.

    Both failure modes — unknown address and wrong password — return the same error
    and take comparable time, so the endpoint cannot be used to discover which
    addresses have accounts.
    """
    user = await user_repo.get_by_email(session, payload.email)

    if user is None:
        # Hash anyway, so a missing user is not measurably faster than a wrong password.
        verify_dummy_password(payload.password)
        logger.info("login_failed", reason="unknown_email")
        raise _INVALID_CREDENTIALS

    if not verify_password(payload.password, user.password_hash):
        logger.info("login_failed", reason="bad_password", user_id=str(user.id))
        raise _INVALID_CREDENTIALS

    if not user.is_active:
        # Same response as bad credentials: whether an account is disabled is not
        # something an unauthenticated caller should learn.
        logger.info("login_failed", reason="inactive_user", user_id=str(user.id))
        raise _INVALID_CREDENTIALS

    # A successful login is the only moment we hold the plaintext, so it is the only
    # opportunity to upgrade a hash written under weaker parameters.
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)

    token = generate_session_token()
    await session_repo.create(
        session,
        user_id=user.id,
        token_hash=hash_session_token(token),
        expires_at=session_expiry(settings.session_ttl_seconds),
    )
    _set_session_cookie(response, token, settings)

    logger.info("login_succeeded", user_id=str(user.id), role=user.role.value)
    return UserResponse.model_validate(user)


@router.post("/logout", response_model=MessageResponse)
async def logout(
    request: Request,
    response: Response,
    session: DbSession,
    settings: SettingsDep,
) -> MessageResponse:
    """End the current session.

    Revokes the stored row as well as clearing the cookie, so a copy of the token
    captured elsewhere stops working immediately.

    Always reports success: logging out is idempotent, and telling an unauthenticated
    caller that their token was unrecognized leaks information for no benefit.
    """
    token = request.cookies.get(settings.session_cookie_name)
    if token:
        record = await session_repo.get_by_token_hash(session, hash_session_token(token))
        if record is not None and record.revoked_at is None:
            await session_repo.revoke(session, record)
            logger.info("logout", user_id=str(record.user_id))

    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
    )
    return MessageResponse(detail="Logged out")


@router.get("/me", response_model=UserResponse)
async def me(user: CurrentUser) -> UserResponse:
    """The authenticated user. The frontend uses this to resolve session state."""
    return UserResponse.model_validate(user)
