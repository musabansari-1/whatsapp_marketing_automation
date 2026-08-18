"""Request and response models.

Separate from the ORM models on purpose: `password_hash` must never be serializable
into a response, and a request body must never be able to set `role` or `is_active`
on a path that does not intend it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.domain.enums import UserRole

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
