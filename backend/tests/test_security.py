"""Password hashing and session token tests."""

from __future__ import annotations

import argon2

from app.core.security import (
    generate_session_token,
    hash_password,
    hash_session_token,
    needs_rehash,
    tokens_match,
    verify_dummy_password,
    verify_password,
)

PASSWORD = "correct horse battery staple"  # noqa: S105 - test fixture, not a credential


class TestPasswordHashing:
    def test_hash_is_argon2id(self) -> None:
        """Argon2id specifically: 'i' resists side channels, 'd' resists GPUs, and
        'id' is the hybrid OWASP recommends for passwords."""
        assert hash_password(PASSWORD).startswith("$argon2id$")

    def test_hash_does_not_contain_the_password(self) -> None:
        assert PASSWORD not in hash_password(PASSWORD)

    def test_correct_password_verifies(self) -> None:
        assert verify_password(PASSWORD, hash_password(PASSWORD)) is True

    def test_wrong_password_is_rejected(self) -> None:
        assert verify_password("wrong", hash_password(PASSWORD)) is False

    def test_hashes_are_salted(self) -> None:
        """Two hashes of one password must differ, or identical passwords would be
        identifiable across accounts from the hashes alone."""
        first, second = hash_password(PASSWORD), hash_password(PASSWORD)
        assert first != second
        assert verify_password(PASSWORD, first)
        assert verify_password(PASSWORD, second)

    def test_malformed_hash_is_rejected_not_raised(self) -> None:
        assert verify_password(PASSWORD, "not-a-hash") is False

    def test_empty_password_does_not_verify_against_real_hash(self) -> None:
        assert verify_password("", hash_password(PASSWORD)) is False

    def test_unicode_passwords_round_trip(self) -> None:
        secret = "пароль-密码-🔐"  # noqa: S105 - test fixture
        assert verify_password(secret, hash_password(secret)) is True


class TestHashParameters:
    def test_meets_owasp_minimums(self) -> None:
        """OWASP's Argon2id floor is m=19 MiB, t=2, p=1."""
        encoded = hash_password(PASSWORD)
        params = argon2.extract_parameters(encoded)
        assert params.memory_cost >= 19456
        assert params.time_cost >= 2
        assert params.parallelism >= 1

    def test_current_hash_does_not_need_rehash(self) -> None:
        assert needs_rehash(hash_password(PASSWORD)) is False

    def test_weaker_hash_is_flagged_for_rehash(self) -> None:
        """A hash written under weaker parameters must be upgraded on next login."""
        weak = argon2.PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PASSWORD)
        assert needs_rehash(weak) is True

    def test_malformed_hash_is_flagged_for_rehash(self) -> None:
        assert needs_rehash("garbage") is True


class TestTimingEqualisation:
    def test_dummy_verification_does_not_raise(self) -> None:
        """The unknown-email path calls this so it costs what a real check costs;
        it must never surface an error to the caller."""
        verify_dummy_password("anything at all")


class TestSessionTokens:
    def test_tokens_are_unique(self) -> None:
        assert generate_session_token() != generate_session_token()

    def test_tokens_carry_enough_entropy(self) -> None:
        # 32 random bytes, url-safe base64 encoded.
        assert len(generate_session_token()) >= 43

    def test_hash_is_deterministic(self) -> None:
        token = generate_session_token()
        assert hash_session_token(token) == hash_session_token(token)

    def test_hash_is_sha256_hex(self) -> None:
        digest = hash_session_token(generate_session_token())
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)

    def test_hash_does_not_reveal_the_token(self) -> None:
        """Only the digest is stored, so a database leak yields no live sessions."""
        token = generate_session_token()
        assert token not in hash_session_token(token)

    def test_different_tokens_hash_differently(self) -> None:
        assert hash_session_token("a") != hash_session_token("b")

    def test_constant_time_comparison(self) -> None:
        digest = hash_session_token("token")
        assert tokens_match(digest, digest) is True
        assert tokens_match(digest, hash_session_token("other")) is False
