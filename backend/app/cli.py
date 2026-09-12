"""Administrative commands.

Run with `python -m app.cli <command>`:

    bootstrap-admin         create the first ADMIN from configuration ([D15])
    configure-products      apply a product catalog file (Phase 27)
    import-legacy-config    copy the pre-Phase-27 environment configuration into one product
    product-readiness       report whether each product can safely be activated
    activate-product        activate one product, after its readiness check
    deactivate-product      stop one product starting new customer activity

None of them calls Meta, Google or HubSpot, and none sends a message. Configuration and
activation are separate on purpose: no amount of configuration activates a product.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from sqlalchemy.exc import IntegrityError

from app.configuration.catalog import CatalogError, apply_catalog, load_catalog
from app.configuration.legacy import (
    LegacyEnvironment,
    describe_sources,
    import_legacy_configuration,
    read_legacy_configuration,
)
from app.configuration.report import platform_notes, render_json, render_product, render_text
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.core.security import hash_password
from app.domain.enums import UserRole
from app.domain.readiness import Verdict
from app.persistence.database import dispose_engine, get_session_factory
from app.persistence.repositories import products as product_repo
from app.persistence.repositories import readiness as readiness_repo
from app.persistence.repositories import users as user_repo

logger = get_logger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1


def _say(out: TextIO | None, text: str) -> None:
    (out or sys.stdout).write(text + "\n")


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


async def configure_products(
    settings: Settings, path: Path, *, dry_run: bool = False, out: TextIO | None = None
) -> int:
    """Apply a product catalog. Idempotent; never changes whether a product is active."""
    try:
        catalog = load_catalog(path)
    except CatalogError as exc:
        _say(out, f"Refused: {exc}")
        return EXIT_FAILED

    platform = readiness_repo.platform_inputs_from_settings(settings)
    factory = get_session_factory()
    async with factory() as session:
        try:
            changes = await apply_catalog(session, catalog, platform=platform)
        except (CatalogError, IntegrityError) as exc:
            await session.rollback()
            _say(out, f"Refused, nothing was written: {exc}")
            return EXIT_FAILED
        if dry_run:
            await session.rollback()
        else:
            await session.commit()

    _say(out, "Dry run: nothing was written." if dry_run else f"Applied {path}.")
    _say(
        out,
        "\n".join(f"  {change}" for change in changes)
        if changes
        else "  No changes: the database already matches the catalog.",
    )
    logger.info("products_configured", changes=len(changes), dry_run=dry_run)
    return EXIT_OK


async def import_legacy_config(
    settings: Settings,
    slug: str,
    *,
    overwrite: bool = False,
    dry_run: bool = False,
    environment: LegacyEnvironment | None = None,
    out: TextIO | None = None,
) -> int:
    """Copy the retired single-product environment configuration into the named product."""
    try:
        legacy = read_legacy_configuration(environment or LegacyEnvironment())
    except CatalogError as exc:
        _say(out, f"Refused: {exc}")
        return EXIT_FAILED

    _say(out, f"Importing the legacy environment configuration into {slug}:")
    _say(out, "\n".join(describe_sources(legacy)))

    platform = readiness_repo.platform_inputs_from_settings(settings)
    factory = get_session_factory()
    async with factory() as session:
        product = await product_repo.get_by_slug(session, slug)
        if product is None:
            _say(out, f"Refused: no product has the slug {slug!r}.")
            return EXIT_FAILED
        try:
            changes = await import_legacy_configuration(
                session, product, legacy, overwrite=overwrite, platform=platform
            )
        except (CatalogError, IntegrityError) as exc:
            await session.rollback()
            _say(out, f"Refused, nothing was written: {exc}")
            return EXIT_FAILED
        if dry_run:
            await session.rollback()
        else:
            await session.commit()

    _say(out, "Dry run: nothing was written." if dry_run else "Imported.")
    _say(
        out,
        "\n".join(f"  {change}" for change in changes)
        if changes
        else f"  No changes: {slug} already holds this configuration.",
    )
    logger.info("legacy_configuration_imported", product=slug, changes=len(changes))
    return EXIT_OK


async def product_readiness(
    settings: Settings,
    slugs: Sequence[str] = (),
    *,
    as_json: bool = False,
    out: TextIO | None = None,
) -> int:
    """Report readiness. Fails only when an **active** product is not ready: an inactive
    product that is still being configured is the expected state, not an error."""
    platform = readiness_repo.platform_inputs_from_settings(settings)
    factory = get_session_factory()
    async with factory() as session:
        try:
            reports = await readiness_repo.evaluate_products(session, platform, slugs)
        except readiness_repo.UnknownProductError as exc:
            _say(out, f"Refused: {exc}")
            return EXIT_FAILED
        product_count = await product_repo.count_products(session)

    notes = platform_notes(settings, product_count=product_count)
    _say(out, render_json(reports, notes) if as_json else render_text(reports, notes))
    return EXIT_FAILED if any(report.unsafe for report in reports) else EXIT_OK


async def activate_product(
    settings: Settings, slug: str, *, accept_gaps: bool = False, out: TextIO | None = None
) -> int:
    """Activate a product, only once readiness allows it.

    NOT READY is refused outright. READY WITH GAPS needs `accept_gaps`, so activating a
    product with no campaign template or no knowledge is a decision someone made, not one
    they missed.
    """
    platform = readiness_repo.platform_inputs_from_settings(settings)
    factory = get_session_factory()
    async with factory() as session:
        product = await product_repo.get_by_slug(session, slug)
        if product is None:
            _say(out, f"Refused: no product has the slug {slug!r}.")
            return EXIT_FAILED
        if product.is_active:
            _say(out, f"{slug} is already active. Nothing changed.")
            return EXIT_OK

        report = await readiness_repo.evaluate_product(session, product, platform)
        _say(out, render_product(report))

        if report.verdict is Verdict.NOT_READY:
            _say(out, f"Refused: {slug} is not ready. It stays inactive.")
            return EXIT_FAILED
        if report.verdict is Verdict.READY_WITH_GAPS and not accept_gaps:
            _say(
                out,
                f"Refused: {slug} has gaps. Re-run with --accept-gaps to activate it anyway. "
                "It stays inactive.",
            )
            return EXIT_FAILED

        product.is_active = True
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            _say(out, f"Refused: another active product holds this number or calendar: {exc}")
            return EXIT_FAILED

    _say(out, f"Activated {slug}.")
    logger.info("product_activated", product=slug, gaps=len(report.gaps))
    return EXIT_OK


async def deactivate_product(settings: Settings, slug: str, *, out: TextIO | None = None) -> int:
    """Stop a product starting new customer activity. Its records stay readable (Phase 26)."""
    factory = get_session_factory()
    async with factory() as session:
        product = await product_repo.get_by_slug(session, slug)
        if product is None:
            _say(out, f"Refused: no product has the slug {slug!r}.")
            return EXIT_FAILED
        if not product.is_active:
            _say(out, f"{slug} is already inactive. Nothing changed.")
            return EXIT_OK
        product.is_active = False
        await session.commit()

    _say(out, f"Deactivated {slug}. Its records remain readable; it starts no new activity.")
    logger.info("product_deactivated", product=slug)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.cli", description="Administrative commands")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser(
        "bootstrap-admin",
        help="create the first ADMIN from BOOTSTRAP_ADMIN_* configuration",
    )

    configure = commands.add_parser("configure-products", help="apply a product catalog file")
    configure.add_argument("--file", required=True, type=Path, help="the catalog JSON file")
    configure.add_argument("--dry-run", action="store_true", help="report changes, write nothing")

    legacy = commands.add_parser(
        "import-legacy-config",
        help="copy the pre-Phase-27 environment configuration into one product",
    )
    legacy.add_argument("slug", help="the product the environment configuration belongs to")
    legacy.add_argument("--overwrite", action="store_true", help="replace differing values")
    legacy.add_argument("--dry-run", action="store_true", help="report changes, write nothing")

    readiness = commands.add_parser(
        "product-readiness", help="report whether products can safely be activated"
    )
    readiness.add_argument("slugs", nargs="*", help="products to report; all when omitted")
    readiness.add_argument("--json", action="store_true", help="machine-readable output")

    activate = commands.add_parser("activate-product", help="activate a ready product")
    activate.add_argument("slug")
    activate.add_argument(
        "--accept-gaps",
        action="store_true",
        help="activate a product that is ready but has gaps (no template, no knowledge)",
    )

    deactivate = commands.add_parser("deactivate-product", help="deactivate a product")
    deactivate.add_argument("slug")

    return parser


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(settings)
    try:
        match args.command:
            case "bootstrap-admin":
                return await bootstrap_admin(settings)
            case "configure-products":
                return await configure_products(settings, args.file, dry_run=args.dry_run)
            case "import-legacy-config":
                return await import_legacy_config(
                    settings, args.slug, overwrite=args.overwrite, dry_run=args.dry_run
                )
            case "product-readiness":
                return await product_readiness(settings, args.slugs, as_json=args.json)
            case "activate-product":
                return await activate_product(settings, args.slug, accept_gaps=args.accept_gaps)
            case "deactivate-product":
                return await deactivate_product(settings, args.slug)
            case _:
                logger.error("unknown_command", command=args.command)
                return EXIT_FAILED
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
