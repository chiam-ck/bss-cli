"""StripeTokenizerAdapter unit tests (v0.16 Track 1).

Uses a fake ``stripe`` SDK at module-level via monkeypatching of the
SDK's ``create`` / ``attach`` classmethods. The Track 0 sandbox fixture
at ``fixtures/stripe_payment_intent_sample.json`` is the response shape
the fake returns — so this test verifies the adapter's parsing against
the redacted-but-real Stripe response, not against an imagined shape.

This is the v0.16 Track 0 doctrine carried into Track 1: write the
parser AGAINST the captured fixture, then test it AGAINST the captured
fixture.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import AsyncIterator
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import stripe
from app.config import Settings
from app.domain.stripe_tokenizer import StripeConfig, StripeTokenizerAdapter
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

PI_FIXTURE = json.loads((FIXTURES / "stripe_payment_intent_sample.json").read_text())
CUS_FIXTURE = json.loads((FIXTURES / "stripe_customer_sample.json").read_text())


@pytest.fixture
def cfg():
    return StripeConfig(
        api_key="sk_test_xxx",
        publishable_key="pk_test_xxx",
        webhook_secret="whsec_xxx",
        allow_test_card_reuse=False,
    )


@pytest_asyncio.fixture
async def session_factory(settings: Settings) -> AsyncIterator:
    engine = create_async_engine(settings.db_url, pool_size=2, max_overflow=2)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def fake_stripe(monkeypatch):
    """Replace stripe.PaymentIntent.create / Customer.create / PaymentMethod.attach
    with AsyncMocks (called via asyncio.to_thread). The mocks return
    objects shaped like the Track 0 fixtures.
    """

    class _FakePI:
        def __init__(self, data):
            self._data = data

        def to_dict(self):
            return self._data

    pi_call = AsyncMock(return_value=_FakePI(PI_FIXTURE))
    monkeypatch.setattr(
        stripe.PaymentIntent,
        "create",
        lambda **kwargs: pi_call(**kwargs)._mock_value if False else PI_FIXTURE,
    )

    cus_call = AsyncMock()
    monkeypatch.setattr(
        stripe.Customer,
        "create",
        lambda **kwargs: CUS_FIXTURE,
    )

    attach_call = AsyncMock()
    monkeypatch.setattr(
        stripe.PaymentMethod,
        "attach",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        stripe.PaymentMethod,
        "detach",
        lambda *args, **kwargs: None,
    )

    return {"pi_call": pi_call, "cus_call": cus_call, "attach_call": attach_call}


class TestChargeSuccess:
    async def test_returns_approved_with_provider_call_id_from_fixture(
        self, cfg, session_factory, fake_stripe
    ):
        adapter = StripeTokenizerAdapter(
            config=cfg, session_factory=session_factory
        )
        result = await adapter.charge(
            "pm_FX_PM001",
            Decimal("10.00"),
            "SGD",
            idempotency_key="ATT-1-r0",
            purpose="bundle_initial",
            customer_external_ref="cus_FX_CUS001",
        )
        assert result.status == "approved"
        assert result.provider_call_id == PI_FIXTURE["id"]
        assert result.gateway_ref == PI_FIXTURE["id"]
        assert result.decline_code is None
        assert result.reason is None


class TestChargeRequiresCustomerExternalRef:
    async def test_missing_customer_ref_raises(
        self, cfg, session_factory, fake_stripe
    ):
        adapter = StripeTokenizerAdapter(
            config=cfg, session_factory=session_factory
        )
        with pytest.raises(ValueError, match="customer_external_ref"):
            await adapter.charge(
                "pm_FX_PM001",
                Decimal("10.00"),
                "SGD",
                idempotency_key="ATT-1-r0",
                purpose="bundle_initial",
                customer_external_ref=None,
            )

    async def test_zero_amount_raises(self, cfg, session_factory, fake_stripe):
        adapter = StripeTokenizerAdapter(
            config=cfg, session_factory=session_factory
        )
        with pytest.raises(ValueError, match="amount must be positive"):
            await adapter.charge(
                "pm_FX_PM001",
                Decimal("0"),
                "SGD",
                idempotency_key="ATT-1-r0",
                purpose="bundle_initial",
                customer_external_ref="cus_FX_CUS001",
            )


class TestTokenizeForbidden:
    async def test_tokenize_raises_loudly(self, cfg, session_factory):
        adapter = StripeTokenizerAdapter(
            config=cfg, session_factory=session_factory
        )
        with pytest.raises(NotImplementedError, match="forbidden in production"):
            await adapter.tokenize("4242424242424242", 12, 2030, "123")
