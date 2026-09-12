"""Product scope for the dashboard API (§28, Phase 26).

Every product-scoped route lives under `/api/v1/p/{product_slug}/…`, and this module is
how a route gets its product. The address is the enforcement: a product-scoped request
cannot omit its product, because the product is part of the path — and
`tests/test_route_scope.py` fails any business route added outside that prefix.

Three rules:

- **An unknown slug is 404.** Authentication runs first, so an anonymous caller learns
  nothing about which slugs exist.
- **A resource belonging to another product is 404, never 403** — the same answer as one
  that does not exist, so one product's contents cannot be probed from another's URL.
- **Inactive products stay readable.** Only routes that start customer activity depend
  on `ActiveProductScope`; the rule itself is `app.domain.product`.

The slug is a permanent route identifier and nothing more. No behaviour here, or
anywhere, branches on which product it is.
"""

from __future__ import annotations

import uuid
from typing import Annotated, TypeVar

from fastapi import Depends, HTTPException, Query, status

from app.api.deps import CurrentUser, DbSession
from app.domain.product import ProductInactiveError, assert_accepts_new_activity
from app.persistence.models import Product, Segment
from app.persistence.repositories import products as product_repo

#: The prefix every product-scoped router mounts under.
PRODUCT_PREFIX = "/p/{product_slug}"

_Row = TypeVar("_Row")


async def get_product(product_slug: str, user: CurrentUser, session: DbSession) -> Product:
    """The product named in the path, or 404.

    Depends on the current user so that authentication is decided before the slug is
    looked up.
    """
    product = await product_repo.get_by_slug(session, product_slug)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    return product


ProductScope = Annotated[Product, Depends(get_product)]


async def require_active_product(product: ProductScope) -> Product:
    """The product, provided it may start new customer activity.

    409 rather than 403: nothing is wrong with the caller's permissions. The product's
    state is what refuses, and it would accept the same request once reactivated.
    """
    try:
        assert_accepts_new_activity(is_active=product.is_active, display_name=product.display_name)
    except ProductInactiveError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return product


ActiveProductScope = Annotated[Product, Depends(require_active_product)]


async def get_segment_filter(
    product: ProductScope,
    session: DbSession,
    segment: str | None = Query(
        default=None,
        max_length=64,
        description="A segment slug within this product. Omit for all segments.",
    ),
) -> Segment | None:
    """An optional segment filter, resolved within the product.

    A segment is a filter inside a product, never a second isolation boundary, so it is
    looked up only among this product's segments: another product's slug is simply not
    found. Omitting it means every segment, including rows that have none.
    """
    if not segment:
        return None
    found = await product_repo.get_segment_by_slug(session, product_id=product.id, slug=segment)
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Segment not found")
    return found


SegmentFilter = Annotated[Segment | None, Depends(get_segment_filter)]


async def segment_for_write(
    session: DbSession, product: Product, segment_id: uuid.UUID | None
) -> uuid.UUID | None:
    """Validate a segment chosen for a new row. It must belong to this product.

    A foreign or unknown segment is 404, for the same reason as any other foreign
    resource. The database refuses the pairing too, through composite foreign keys; this
    exists so the caller gets an answer rather than an integrity error.
    """
    if segment_id is None:
        return None
    segment = await product_repo.get_segment(session, segment_id)
    if segment is None or segment.product_id != product.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Segment not found")
    return segment.id


def owned_by(row: _Row | None, product: Product, *, not_found: str) -> _Row:
    """The row, if it belongs to this product; otherwise the same 404 as a missing one."""
    if row is None or getattr(row, "product_id", None) != product.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=not_found)
    return row


__all__ = [
    "PRODUCT_PREFIX",
    "ActiveProductScope",
    "ProductScope",
    "SegmentFilter",
    "get_product",
    "get_segment_filter",
    "owned_by",
    "require_active_product",
    "segment_for_write",
]
