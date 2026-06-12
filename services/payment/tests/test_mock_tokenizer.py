"""Unit tests for mock_tokenizer — pure functions, no DB, no HTTP."""

import pytest
from app.domain.mock_tokenizer import ChargeResult, TokenizeResult, charge, tokenize_card


class TestTokenizeCard:
    def test_visa_brand(self):
        result = tokenize_card("4242424242424242", 12, 2030, "123")
        assert result.brand == "visa"
        assert result.last4 == "4242"
        assert result.token.startswith("tok_")

    def test_mastercard_brand(self):
        result = tokenize_card("5105105105105100", 12, 2030, "123")
        assert result.brand == "mastercard"
        assert result.last4 == "5100"

    def test_amex_brand_34(self):
        result = tokenize_card("340000000000009", 12, 2030, "1234")
        assert result.brand == "amex"

    def test_amex_brand_37(self):
        result = tokenize_card("370000000000002", 12, 2030, "1234")
        assert result.brand == "amex"

    def test_unknown_brand(self):
        result = tokenize_card("9999999999999999", 12, 2030, "123")
        assert result.brand == "unknown"

    def test_fail_token_embeds_fail(self):
        result = tokenize_card("4242FAIL42424242", 12, 2030, "123")
        assert "FAIL" in result.token

    def test_decline_token_embeds_decline(self):
        result = tokenize_card("4242DECLINE42424242", 12, 2030, "123")
        assert "DECLINE" in result.token

    def test_expired_card_raises(self):
        with pytest.raises(ValueError, match="expired"):
            tokenize_card("4242424242424242", 1, 2020, "123")

    def test_unique_tokens(self):
        t1 = tokenize_card("4242424242424242", 12, 2030, "123")
        t2 = tokenize_card("4242424242424242", 12, 2030, "123")
        assert t1.token != t2.token

    def test_returns_tokenize_result(self):
        result = tokenize_card("4242424242424242", 12, 2030, "123")
        assert isinstance(result, TokenizeResult)


class TestCharge:
    @pytest.mark.asyncio
    async def test_approved(self):
        from decimal import Decimal

        result = await charge("tok_abc123", Decimal("10.00"), "SGD")
        assert result.status == "approved"
        assert result.reason is None
        assert result.gateway_ref.startswith("mock_")
        assert isinstance(result, ChargeResult)

    @pytest.mark.asyncio
    async def test_declined_fail_token(self):
        from decimal import Decimal

        result = await charge("tok_FAIL_abc123", Decimal("10.00"), "SGD")
        assert result.status == "declined"
        assert result.reason == "card_declined_by_issuer"

    @pytest.mark.asyncio
    async def test_declined_decline_token(self):
        from decimal import Decimal

        result = await charge("tok_DECLINE_abc123", Decimal("10.00"), "SGD")
        assert result.status == "declined"

    @pytest.mark.asyncio
    async def test_zero_amount_raises(self):
        from decimal import Decimal

        with pytest.raises(ValueError, match="positive"):
            await charge("tok_abc123", Decimal("0"), "SGD")

    @pytest.mark.asyncio
    async def test_negative_amount_raises(self):
        from decimal import Decimal

        with pytest.raises(ValueError, match="positive"):
            await charge("tok_abc123", Decimal("-5.00"), "SGD")
