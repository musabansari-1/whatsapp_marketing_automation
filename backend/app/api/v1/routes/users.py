"""Internal user management ([D2], [D14]).

Every route here is ADMIN-only. There is deliberately no registration endpoint: [D2]
and [D15] are explicit that accounts are created by an administrator or by the
one-time bootstrap command, never by a public caller.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from app.api.deps import AdminUser, DbSession
from app.api.schemas import CreateUserRequest, UpdateUserRequest, UserResponse
from app.core.logging import get_logger
from app.core.security import hash_password
from app.domain.auth import can_deactivate
from app.persistence.repositories import sessions as session_repo
from app.persistence.repositories import users as user_repo

router = APIRouter(prefix="/users", tags=["users"])
logger = get_logger(__name__)


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: CreateUserRequest,
    admin: AdminUser,
    session: DbSession,
) -> UserResponse:
    existing = await user_repo.get_by_email(session, payload.email)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with that email already exists",
        )

    user = await user_repo.create(
        session,
        email=payload.email,
        password_hash=hash_password(payload.password),
        role=payload.role,
        full_name=payload.full_name,
    )
    logger.info(
        "user_created",
        created_user_id=str(user.id),
        role=user.role.value,
        by_user_id=str(admin.id),
    )
    return UserResponse.model_validate(user)


@router.get("", response_model=list[UserResponse])
async def list_users(admin: AdminUser, session: DbSession) -> list[UserResponse]:
    users = await user_repo.list_users(session)
    return [UserResponse.model_validate(user) for user in users]


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(user_id: uuid.UUID, admin: AdminUser, session: DbSession) -> UserResponse:
    user = await user_repo.get_by_id(session, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return UserResponse.model_validate(user)


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: uuid.UUID,
    payload: UpdateUserRequest,
    admin: AdminUser,
    session: DbSession,
) -> UserResponse:
    """Update a user. Omitted fields are left unchanged.

    Two side effects are deliberate: deactivating a user revokes their live sessions
    immediately, and changing a password does the same, so a compromised session
    cannot outlive the credential change.
    """
    user = await user_repo.get_by_id(session, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if payload.is_active is False and not can_deactivate(admin.id, user.id):
        # Guards against an administrator locking themselves — potentially everyone —
        # out, leaving the bootstrap command as the only recovery.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot deactivate your own account",
        )

    if payload.full_name is not None:
        user.full_name = payload.full_name
    if payload.role is not None:
        user.role = payload.role

    revoke_sessions = False
    if payload.password is not None:
        user.password_hash = hash_password(payload.password)
        revoke_sessions = True
    if payload.is_active is not None:
        user.is_active = payload.is_active
        if payload.is_active is False:
            revoke_sessions = True

    await session.flush()

    if revoke_sessions:
        revoked = await session_repo.revoke_all_for_user(session, user.id)
        logger.info("sessions_revoked", target_user_id=str(user.id), count=revoked)

    logger.info("user_updated", target_user_id=str(user.id), by_user_id=str(admin.id))
    return UserResponse.model_validate(user)
