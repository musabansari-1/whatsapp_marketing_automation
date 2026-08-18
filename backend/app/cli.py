"""Administrative commands.

Run with `python -m app.cli <command>`.

Currently one command: the first-ADMIN bootstrap required by [D15]. It exists because
[D2] forbids public registration, which leaves a chicken-and-egg problem — only an
admin can create users, and initially there is no admin.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.core.security import hash_password
from app.domain.enums import UserRole
from app.persistence.database import dispose_engine, get_session_factory
from app.persistence.repositories import users as user_repo

logger = get_logger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1


async def bootstrap_admin(settings: Settings) -> int:
    """Create the first ADMIN from configuration.

    Idempotent and guarded ([D15]): if any ADMIN already exists the command makes no
    change and reports success, so re-running it during a deploy is harmless and it
    can never be used to add a second administrator behind the API's back.

    Credentials come from secret configuration, never from arguments — a password in
    argv is visible in the process list and shell history.
    """
    email = settings.bootstrap_admin_email
    password = settings.bootstrap_admin_password

    if not email or not password:
        logger.error(
            "bootstrap_admin_missing_configuration",
            hint="Set BOOTSTRAP_ADMIN_EMAIL and BOOTSTRAP_ADMIN_PASSWORD",
        )
        return EXIT_FAILED

    factory = get_session_factory()
    async with factory() as session:
        if await user_repo.admin_exists(session):
            logger.info("bootstrap_admin_skipped", reason="an admin already exists")
            return EXIT_OK

        if await user_repo.get_by_email(session, email) is not None:
            logger.error("bootstrap_admin_failed", reason="email already in use")
            return EXIT_FAILED

        user = await user_repo.create(
            session,
            email=email,
            password_hash=hash_password(password.get_secret_value()),
            role=UserRole.ADMIN,
            is_active=True,
        )
        await session.commit()

    logger.info("bootstrap_admin_created", user_id=str(user.id), email=email)
    return EXIT_OK


async def _run(command: str) -> int:
    settings = get_settings()
    configure_logging(settings)
    try:
        if command == "bootstrap-admin":
            return await bootstrap_admin(settings)
        logger.error("unknown_command", command=command)
        return EXIT_FAILED
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli", description="Administrative commands")
    parser.add_argument(
        "command",
        choices=["bootstrap-admin"],
        help="bootstrap-admin: create the first ADMIN from BOOTSTRAP_ADMIN_* configuration",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.command))


if __name__ == "__main__":
    sys.exit(main())
