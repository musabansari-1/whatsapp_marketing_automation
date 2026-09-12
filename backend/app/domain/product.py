"""Product lifecycle rules (§28, Phase 26).

Pure logic: the caller says what it knows about a product, and this decides.

**An inactive product is readable and configurable, but starts nothing.** Deactivating a
product never means deleting it: its leads, conversations, bookings and campaigns stay
open to inspection. And a product has to be preparable before it goes live — templates
assigned, knowledge uploaded, calendar recorded — so configuration stays allowed.

What an inactive product may not do is begin activity that reaches customers or creates
customer records:

    audience upload and confirmation     creates the leads a campaign would message
    campaign creation and sending        messages customers
    booking a demo                       commits a salesperson's time to a customer
    receiving an inbound message         routed only to active products since Phase 23

Stopping activity already under way — cancelling a campaign, archiving a document —
stays allowed. Refusing it would keep alive exactly what deactivation was meant to end.

Nothing here branches on which product it is. The rule reads one flag.
"""

from __future__ import annotations


class ProductInactiveError(Exception):
    """The product is inactive, so it cannot start new customer activity."""


def assert_accepts_new_activity(*, is_active: bool, display_name: str) -> None:
    """Refuse new customer activity for an inactive product.

    One function, called from every place that starts such activity — the API routes
    that create audiences and campaigns, the campaign send worker, and booking — so the
    rule is stated once rather than re-implemented as a scattering of flag checks.
    """
    if not is_active:
        raise ProductInactiveError(
            f"{display_name} is inactive, so it cannot start new customer activity. "
            "Its existing records remain available."
        )


__all__ = ["ProductInactiveError", "assert_accepts_new_activity"]
