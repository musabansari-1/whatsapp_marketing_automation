"""Product lifecycle rule (§28, Phase 26).

An inactive product stays readable and configurable, and starts no new customer
activity. The rule is one function so every place that starts such activity asks the
same question.
"""

from __future__ import annotations

import pytest

from app.domain.product import ProductInactiveError, assert_accepts_new_activity


class TestNewActivity:
    def test_an_active_product_accepts_it(self) -> None:
        assert_accepts_new_activity(is_active=True, display_name="Any product")

    def test_an_inactive_product_refuses_it(self) -> None:
        with pytest.raises(ProductInactiveError):
            assert_accepts_new_activity(is_active=False, display_name="Any product")

    def test_the_refusal_names_the_product_and_what_stays_available(self) -> None:
        """An operator reading the error should know which product and that nothing was
        lost — deactivation is not deletion."""
        with pytest.raises(ProductInactiveError) as caught:
            assert_accepts_new_activity(is_active=False, display_name="Second Product")

        message = str(caught.value)
        assert "Second Product" in message
        assert "existing records remain available" in message

    def test_the_rule_reads_the_flag_and_nothing_else(self) -> None:
        """No product is special. The same flag gives the same answer for any name."""
        for name in ("First", "Second", "Third"):
            assert_accepts_new_activity(is_active=True, display_name=name)
            with pytest.raises(ProductInactiveError):
                assert_accepts_new_activity(is_active=False, display_name=name)
