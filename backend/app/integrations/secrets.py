"""Secret storage ([D19], specification §19).

[D19] puts the Google OAuth refresh token in Google Secret Manager, never in the
database and never in plaintext configuration. This is the seam that makes that
possible without dragging Secret Manager into local development or the test suite,
which [D19] also forbids.

Two operations, and the second is the one that is easy to forget: Google can return a
*rotated* refresh token, and a worker that does not write the new one back will
authenticate fine until the old token expires and then fail everywhere at once. So the
store writes as well as reads, and the worker's service account needs both permissions.

Values are never logged. A secret in an error message is a secret in the log store.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.core.errors import NonRetryableProviderError, RetryableProviderError
from app.core.logging import get_logger

logger = get_logger(__name__)


class SecretStore(ABC):
    """Reads and writes secret values by name."""

    name: str

    @abstractmethod
    async def get_secret(self, secret_name: str) -> str | None:
        """The current value, or None when the secret does not exist."""

    @abstractmethod
    async def set_secret(self, secret_name: str, value: str) -> None:
        """Store a new version. Previous versions are left in place."""


class InMemorySecretStore(SecretStore):
    """For local development and tests ([D19] requires neither to need GCP)."""

    name = "memory"

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values: dict[str, str] = dict(values or {})
        # Recorded so a test can assert that a rotated token was written back — the
        # failure mode that only shows up months later.
        self.writes: list[str] = []

    async def get_secret(self, secret_name: str) -> str | None:
        return self.values.get(secret_name)

    async def set_secret(self, secret_name: str, value: str) -> None:
        self.values[secret_name] = value
        self.writes.append(secret_name)


class GoogleSecretManagerStore(SecretStore):
    """Google Secret Manager ([D19]).

    The client is created lazily and reused: constructing it reads credentials from
    the environment, which the API service should not pay for when it never touches a
    secret.
    """

    name = "google_secret_manager"

    def __init__(self, *, project_id: str, client: Any | None = None) -> None:
        if not project_id:
            raise ValueError("Secret Manager requires a project ID")
        self._project_id = project_id
        self._client = client

    async def get_secret(self, secret_name: str) -> str | None:
        import asyncio

        return await asyncio.to_thread(self._get_sync, secret_name)

    async def set_secret(self, secret_name: str, value: str) -> None:
        import asyncio

        await asyncio.to_thread(self._set_sync, secret_name, value)

    # ---------------------------------------------------------------- internals

    def _get_sync(self, secret_name: str) -> str | None:
        from google.api_core import exceptions as google_exceptions

        client = self._get_client()
        path = f"projects/{self._project_id}/secrets/{secret_name}/versions/latest"

        try:
            response = client.access_secret_version(request={"name": path})
        except google_exceptions.NotFound:
            # Absent rather than broken: the caller decides whether that is fatal.
            return None
        except google_exceptions.PermissionDenied as exc:
            # Retrying with the same rejected identity only burns quota.
            raise NonRetryableProviderError(
                f"Not permitted to read secret {secret_name!r}", provider=self.name
            ) from exc
        except Exception as exc:  # noqa: BLE001 - transport failures are transient
            raise RetryableProviderError(
                f"Could not read secret {secret_name!r}: {type(exc).__name__}",
                provider=self.name,
            ) from exc

        payload: bytes = response.payload.data
        return payload.decode("utf-8")

    def _set_sync(self, secret_name: str, value: str) -> None:
        from google.api_core import exceptions as google_exceptions

        client = self._get_client()
        parent = f"projects/{self._project_id}/secrets/{secret_name}"

        try:
            client.add_secret_version(
                request={"parent": parent, "payload": {"data": value.encode("utf-8")}}
            )
        except google_exceptions.PermissionDenied as exc:
            raise NonRetryableProviderError(
                f"Not permitted to write secret {secret_name!r}", provider=self.name
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise RetryableProviderError(
                f"Could not write secret {secret_name!r}: {type(exc).__name__}",
                provider=self.name,
            ) from exc

        # The name only. The value is the secret.
        logger.info("secret_version_added", secret_name=secret_name)

    def _get_client(self) -> Any:
        if self._client is None:
            from google.cloud import secretmanager

            self._client = secretmanager.SecretManagerServiceClient()
        return self._client


__all__ = ["GoogleSecretManagerStore", "InMemorySecretStore", "SecretStore"]
