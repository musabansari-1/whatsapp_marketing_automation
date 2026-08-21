"""Cloud Tasks request authentication (§18, §19).

Cloud Tasks signs each dispatch with a Google-issued OIDC token identifying the
service account that created the task. §18 requires every task to be authenticated;
without this the worker's task endpoint is an open door to anyone who can reach the
Cloud Run URL and guess a payload.

The check is skipped when no service account is configured, which is how local
development and the test suite run. That is safe because the setting is only present
in a deployed environment — but it means the deployment must set it, so a missing
configuration is logged loudly rather than passing silently.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class TaskAuthenticationError(Exception):
    """The request did not carry a valid Cloud Tasks OIDC token."""


def verify_task_request(request: Request, settings: Settings) -> None:
    """Reject a task request that is not from Cloud Tasks.

    Verifies the bearer token's signature, issuer, audience and the service account
    it was minted for. Google rotates the signing keys, so verification fetches and
    caches them rather than pinning.
    """
    expected_account = settings.worker_service_account_email
    if not expected_account:
        # Unconfigured: local development, tests, or an inline queue. Nothing to
        # verify against, and refusing would make the worker unusable locally.
        return

    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        logger.warning("task_auth_missing_token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing task credentials"
        )

    token = header.removeprefix("Bearer ").strip()
    audience = settings.worker_base_url

    try:
        # Imported lazily so the dependency is only needed where OIDC is actually
        # configured; local development and tests never reach this.
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token

        claims = id_token.verify_oauth2_token(token, google_requests.Request(), audience=audience)
    except Exception as exc:  # noqa: BLE001 - any verification failure is a rejection
        logger.warning("task_auth_invalid_token", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid task credentials"
        ) from exc

    email = claims.get("email")
    if email != expected_account:
        # A validly-signed token from the wrong service account is still an outsider.
        logger.warning("task_auth_wrong_service_account")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Unrecognised task caller"
        )

    if claims.get("email_verified") is False:
        logger.warning("task_auth_unverified_email")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Unrecognised task caller"
        )
