"""Direct policy unit tests with injected mocks."""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from app.policies.base import PolicyViolation
from app.policies.payment import (
    check_customer_matches_method,
    check_method_active,
    check_positive_amount,
    check_token_provider_matches_active,
)
from app.policies.payment_method import (
    check_at_most_n_methods,
    check_card_not_expired,
    check_customer_active_or_pending,
    check_customer_exists,
)


class TestPaymentMethodPolicies:
    @pytest.mark.asyncio
    async def test_customer_exists_ok(self):
        mock_crm = AsyncMock()
        mock_crm.get_customer.return_value = {"id": "CUST-001", "status": "active"}
        result = await check_customer_exists("CUST-001", mock_crm)
        assert result["id"] == "CUST-001"

    @pytest.mark.asyncio
    async def test_customer_exists_not_found(self):
        from bss_clients import NotFound

        mock_crm = AsyncMock()
        mock_crm.get_customer.side_effect = NotFound("not found")
        with pytest.raises(PolicyViolation) as exc_info:
            await check_customer_exists("CUST-999", mock_crm)
        assert exc_info.value.rule == "payment_method.add.customer_exists"

    def test_customer_active_ok(self):
        check_customer_active_or_pending({"status": "active"})

    def test_customer_pending_ok(self):
        check_customer_active_or_pending({"status": "pending"})

    def test_customer_closed_rejected(self):
        with pytest.raises(PolicyViolation) as exc_info:
            check_customer_active_or_pending({"status": "closed"})
        assert exc_info.value.rule == "payment_method.add.customer_active_or_pending"

    def test_customer_suspended_rejected(self):
        with pytest.raises(PolicyViolation):
            check_customer_active_or_pending({"status": "suspended"})

    def test_card_not_expired_ok(self):
        check_card_not_expired(12, 2030)

    def test_card_expired(self):
        with pytest.raises(PolicyViolation) as exc_info:
            check_card_not_expired(1, 2020)
        assert exc_info.value.rule == "payment_method.add.card_not_expired"

    @pytest.mark.asyncio
    async def test_at_most_n_methods_ok(self):
        mock_repo = AsyncMock()
        mock_repo.count_active_for_customer.return_value = 3
        await check_at_most_n_methods("CUST-001", mock_repo)

    @pytest.mark.asyncio
    async def test_at_most_n_methods_exceeded(self):
        mock_repo = AsyncMock()
        mock_repo.count_active_for_customer.return_value = 5
        with pytest.raises(PolicyViolation) as exc_info:
            await check_at_most_n_methods("CUST-001", mock_repo)
        assert exc_info.value.rule == "payment_method.add.at_most_n_methods"


class TestPaymentChargePolicies:
    def _make_method(self, *, status="active", customer_id="CUST-001", method_id="PM-0001"):
        from unittest.mock import MagicMock

        m = MagicMock()
        m.id = method_id
        m.status = status
        m.customer_id = customer_id
        return m

    def test_method_active_ok(self):
        check_method_active(self._make_method())

    def test_method_removed_rejected(self):
        with pytest.raises(PolicyViolation) as exc_info:
            check_method_active(self._make_method(status="removed"))
        assert exc_info.value.rule == "payment.charge.method_active"

    def test_positive_amount_ok(self):
        check_positive_amount(Decimal("10.00"))

    def test_zero_amount_rejected(self):
        with pytest.raises(PolicyViolation) as exc_info:
            check_positive_amount(Decimal("0"))
        assert exc_info.value.rule == "payment.charge.positive_amount"

    def test_negative_amount_rejected(self):
        with pytest.raises(PolicyViolation):
            check_positive_amount(Decimal("-5.00"))

    def test_customer_matches_ok(self):
        check_customer_matches_method("CUST-001", self._make_method())

    def test_customer_mismatch_rejected(self):
        with pytest.raises(PolicyViolation) as exc_info:
            check_customer_matches_method("CUST-999", self._make_method())
        assert exc_info.value.rule == "payment.charge.customer_matches_method"


class TestTokenProviderMatchesActive:
    """v0.16 lazy-fail cutover guard."""

    def _make_method(self, *, token_provider="mock"):
        from unittest.mock import MagicMock

        m = MagicMock()
        m.id = "PM-0001"
        m.token_provider = token_provider
        return m

    def test_mock_row_with_mock_adapter_passes(self):
        check_token_provider_matches_active(
            self._make_method(token_provider="mock"), "MockTokenizerAdapter"
        )

    def test_stripe_row_with_stripe_adapter_passes(self):
        check_token_provider_matches_active(
            self._make_method(token_provider="stripe"), "StripeTokenizerAdapter"
        )

    def test_mock_row_under_stripe_adapter_rejected(self):
        with pytest.raises(PolicyViolation) as exc_info:
            check_token_provider_matches_active(
                self._make_method(token_provider="mock"),
                "StripeTokenizerAdapter",
            )
        assert (
            exc_info.value.rule
            == "payment.charge.token_provider_matches_active"
        )
        assert exc_info.value.context["row_token_provider"] == "mock"
        assert exc_info.value.context["expected_token_provider"] == "stripe"

    def test_stripe_row_under_mock_adapter_rejected(self):
        with pytest.raises(PolicyViolation):
            check_token_provider_matches_active(
                self._make_method(token_provider="stripe"),
                "MockTokenizerAdapter",
            )

    def test_unknown_adapter_class_skipped(self):
        # Test doubles use unknown class names; the guard no-ops so
        # tests can use stub tokenizers without setting token_provider
        # to match.
        check_token_provider_matches_active(
            self._make_method(token_provider="mock"), "FooTokenizer"
        )
