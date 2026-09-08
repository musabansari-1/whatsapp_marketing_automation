"""Product context resolution (§28, Phase 19).

Which product a piece of internal authoring belongs to. Pure logic: the caller supplies
what it knows and what products are active, and this decides — or refuses.

**This is for internal authoring only.** An admin uploading an audience is telling the
platform something; resolving what they meant is legitimate. Inbound customer routing is
the opposite situation — nobody is present to ask — and [D40]/[D45] require it to come
from the receiving `phone_number_id` and to fail closed on an unknown one. Nothing here
may be used for that.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence


class ProductContextError(Exception):
    """The product for this operation cannot be determined."""


def resolve_authoring_product(
    *,
    explicit: uuid.UUID | None,
    active_product_ids: Sequence[uuid.UUID],
) -> uuid.UUID:
    """The product an authoring request applies to.

    An explicit choice always wins. Without one, exactly one active product means there
    is nothing to disambiguate and the request is unambiguous in fact even though it is
    silent — which is what keeps Academicae's existing API working while it is the only
    product ([D54]).

    Zero or several active products make silence genuinely ambiguous, and it is refused.
    The count is what decides, never which product came first: "no product means
    Academicae" would be a default product by another name, and the moment a second
    product exists it would start quietly mis-filing data.

    This compatibility path is transitional. Phase 26 gives the dashboard explicit
    product selection, after which it should be reconsidered.
    """
    if explicit is not None:
        return explicit

    if len(active_product_ids) == 1:
        return active_product_ids[0]

    if not active_product_ids:
        raise ProductContextError(
            "No active product is configured, so this request has no product to belong to."
        )

    raise ProductContextError(
        f"{len(active_product_ids)} active products exist, so this request must name one."
    )


__all__ = ["ProductContextError", "resolve_authoring_product"]
