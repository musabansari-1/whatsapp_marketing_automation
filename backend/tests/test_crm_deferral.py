"""CRM synchronization is deferred while more than one product exists (Phase 26).

The existing CRM properties are flat — `wa_lead_status`, `wa_campaign`, `wa_demo_at` and
the rest carry no product. One HubSpot contact is one human ([D44]) who may be a separate
lead in several products ([D41]), so writing them from more than one product would let
the last sync overwrite every other product's state. The multi-product CRM design is
deferred; this guard is what keeps the deferral from corrupting anything meanwhile.
"""

from __future__ import annotations

import pytest

from app.domain.crm import single_product_sync_is_safe


class TestDeferralGuard:
    @pytest.mark.parametrize("count", [0, 1])
    def test_single_product_sync_stays_available(self, count: int) -> None:
        """A single-product deployment keeps the integration it already had."""
        assert single_product_sync_is_safe(product_count=count) is True

    @pytest.mark.parametrize("count", [2, 3, 10])
    def test_more_than_one_product_defers_sync(self, count: int) -> None:
        assert single_product_sync_is_safe(product_count=count) is False
