"""Password hashing and session token primitives.

Two different one-way functions, for two different reasons:

Passwords use **Argon2id**, deliberately slow and memory-hard, because a user-chosen
password is low-entropy and must survive an offline attack on a stolen hash.

Session tokens use SHA-256, deliberately fast, because the token is 256 bits of
cryptographic randomness. There is nothing to brute-force, so the cost of Argon2 would
buy no security while adding latency to every authenticated request.

Storing only the token hash means a database leak does not hand over live sessions.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# argon2-cffi defaults are Argon2id and exceed the OWASP minimum
# (time_cost=3, memory_cost=64 MiB, parallelism=4). Kept explicit so a future change
# is a visible edit rather than a silent library upgrade.
_password_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,  # KiB
    parallelism=4,
    hash_len=32,
    salt_len=16,
)

# Verified against when no user matches, so a login attempt for an unknown address
# costs the same as one for a known address. Without this, response timing
# distinguishes registered from unregistered emails.
_DUMMY_HASH = _password_hasher.hash("timing-equalisation-placeholder")

SESSION_TOKEN_BYTES = 32


def hash_password(password: str) -> str:
    """Return an Argon2id hash. The encoded form carries its own salt and parameters."""
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Check a password against a hash, returning False rather than raising."""
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def verify_dummy_password(password: str) -> None:
    """Burn equivalent time when no user was found.

    Called on the no-such-user path so login latency does not reveal which addresses
    exist.
    """
    with contextlib.suppress(VerifyMismatchError, VerificationError, InvalidHashError):
        _password_hasher.verify(_DUMMY_HASH, password)


def needs_rehash(password_hash: str) -> bool:
    """True when a stored hash predates the current parameters.

    Lets a successful login transparently upgrade an old hash.
    """
    try:
        return _password_hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def generate_session_token() -> str:
    """A new opaque session token. Returned to the client; never stored as-is."""
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def hash_session_token(token: str) -> str:
    """Hash a session token for storage and lookup."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_match(left: str, right: str) -> bool:
    """Constant-time comparison for token digests."""
    return hmac.compare_digest(left, right)
