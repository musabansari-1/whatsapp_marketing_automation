"""Every API route is product-scoped or deliberately global (§28, Phase 26).

This is the structural guarantee behind "a product-scoped request cannot silently return
every product's rows". Product-scoped routes live under `/api/v1/p/{product_slug}` and
resolve their product through `get_product`. The few global routes are listed here by
name, each with the reason it is global.

A route that is neither fails this test. So a list endpoint added later has to take a
product — or be argued into the global list, in review, with its reason written down.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

import pytest
from fastapi.routing import APIRoute

from app.api.main import create_app
from app.api.scope import get_product
from app.core.config import Settings

SCOPED_PREFIX = "/api/v1/p/{product_slug}"

#: Routes that serve the organisation rather than one product, and why.
GLOBAL_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/health"): "liveness for the platform, not for a product",
    ("GET", "/readiness"): "readiness for the platform, not for a product",
    ("GET", "/api/v1/health"): "liveness for the platform, not for a product",
    ("GET", "/api/v1/readiness"): "readiness for the platform, not for a product",
    ("POST", "/api/v1/auth/login"): "a person signs in once, whichever product they work on",
    ("POST", "/api/v1/auth/logout"): "a session belongs to a person, not a product",
    ("GET", "/api/v1/auth/me"): "a session belongs to a person, not a product",
    ("POST", "/api/v1/users"): "one internal team across every product ([D46])",
    ("GET", "/api/v1/users"): "one internal team across every product ([D46])",
    ("GET", "/api/v1/users/{user_id}"): "one internal team across every product ([D46])",
    ("PATCH", "/api/v1/users/{user_id}"): "one internal team across every product ([D46])",
    ("GET", "/api/v1/webhooks/whatsapp"): "Meta's handshake; no product is involved",
    (
        "POST",
        "/api/v1/webhooks/whatsapp",
    ): "one webhook for the shared WABA; routed by number ([D40])",
    ("GET", "/api/v1/products"): "the list the product switcher chooses from",
    ("GET", "/api/v1/notifications"): "one inbox per person; each notification names its product",
    ("POST", "/api/v1/notifications/{notification_id}/read"): "one inbox per person",
    ("POST", "/api/v1/notifications/read-all"): "one inbox per person",
    (
        "GET",
        "/api/v1/whatsapp-templates/unassigned",
    ): "an unassigned template has no product ([D57])",
    ("GET", "/api/v1/analytics/overview"): "the combined view, requested explicitly ([D53])",
    ("GET", "/api/v1/analytics/leads-by-status"): "the combined view, requested explicitly ([D53])",
}

#: Resources that only ever exist inside a product. No global route may serve them.
PRODUCT_RESOURCES = (
    "audiences",
    "campaigns",
    "leads",
    "conversations",
    "bookings",
    "knowledge-bases",
    "segments",
)


def _walk(routes: Iterable[Any], prefix: str = "") -> Iterator[tuple[str, APIRoute]]:
    """Every API route with its full path.

    Recent FastAPI keeps an included router behind a wrapper rather than flattening its
    routes into the application, so the walk follows each wrapper's prefix down.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
            continue
        context = getattr(route, "include_context", None)
        if context is not None:
            yield from _walk(context.included_router.routes, prefix + context.prefix)


def _resolves_product(dependant: Any) -> bool:
    return any(
        dependency.call is get_product or _resolves_product(dependency)
        for dependency in dependant.dependencies
    )


@pytest.fixture(scope="module")
def routes() -> list[tuple[str, str, APIRoute]]:
    app = create_app(Settings())
    return [
        (method, path, route)
        for path, route in _walk(app.routes)
        for method in sorted(route.methods or ())
    ]


class TestEveryRouteIsClassified:
    def test_the_walk_finds_the_application(self, routes: list[tuple[str, str, APIRoute]]) -> None:
        """Guards the guard: an empty walk would pass every assertion below."""
        scoped = [path for _method, path, _route in routes if path.startswith(SCOPED_PREFIX)]
        assert len(scoped) >= 30
        assert len(routes) >= len(GLOBAL_ROUTES) + len(scoped)

    def test_every_route_is_scoped_or_named_global(
        self, routes: list[tuple[str, str, APIRoute]]
    ) -> None:
        unclassified = [
            f"{method} {path}"
            for method, path, _route in routes
            if not path.startswith(SCOPED_PREFIX) and (method, path) not in GLOBAL_ROUTES
        ]
        assert unclassified == [], (
            "These routes are neither under /p/{product_slug} nor listed as global. "
            "Give them a product, or add them to GLOBAL_ROUTES with the reason."
        )

    def test_every_scoped_route_resolves_its_product(
        self, routes: list[tuple[str, str, APIRoute]]
    ) -> None:
        """The prefix alone is not enough: the route must actually look the product up,
        or a handler could accept the slug and ignore it."""
        unresolved = [
            f"{method} {path}"
            for method, path, route in routes
            if path.startswith(SCOPED_PREFIX) and not _resolves_product(route.dependant)
        ]
        assert unresolved == []

    def test_no_global_route_is_stale(self, routes: list[tuple[str, str, APIRoute]]) -> None:
        """An entry for a route that no longer exists would be an allowance nobody uses."""
        present = {(method, path) for method, path, _route in routes}
        assert set(GLOBAL_ROUTES) - present == set()


class TestProductDataHasOneAddress:
    def test_no_global_route_serves_a_product_resource(self) -> None:
        for (_method, path), reason in GLOBAL_ROUTES.items():
            segments = path.strip("/").split("/")
            assert not set(segments) & set(PRODUCT_RESOURCES), f"{path} ({reason})"

    def test_products_are_addressed_by_slug_only(
        self, routes: list[tuple[str, str, APIRoute]]
    ) -> None:
        """One way to name a product in a URL, so there is one thing to authorise."""
        assert [path for _m, path, _r in routes if "{product_id}" in path] == []

    def test_every_global_route_has_a_reason(self) -> None:
        for route, reason in GLOBAL_ROUTES.items():
            assert reason.strip(), route
