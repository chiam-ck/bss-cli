"""select_tokenizer startup guards (v0.16 Track 1).

Every guard listed in app/domain/select_tokenizer.py and the v0.16 spec
"What v0.16 ships" §1.3 is exercised here. The fail-fast contract is
non-negotiable: silent downgrade to mock when stripe creds are missing
is the v0.14-doctrine bug we're explicitly avoiding.
"""

from __future__ import annotations

import pytest
from app.domain.mock_tokenizer import MockTokenizerAdapter
from app.domain.select_tokenizer import select_tokenizer
from app.domain.stripe_tokenizer import StripeTokenizerAdapter


class _SessionFactoryStub:
    """Minimal placeholder — adapter constructors don't call it at __init__ time."""


class TestMockSelection:
    def test_mock_returns_mock_adapter(self):
        adapter = select_tokenizer(name="mock", env="development")
        assert isinstance(adapter, MockTokenizerAdapter)

    def test_mock_in_production_is_allowed(self):
        # Mock is fine in any env; the production guard is for stripe only.
        adapter = select_tokenizer(name="mock", env="production")
        assert isinstance(adapter, MockTokenizerAdapter)


class TestStripeSelection:
    def _kwargs(self, **overrides):
        base = dict(
            name="stripe",
            env="development",
            stripe_api_key="sk_test_abc123",
            stripe_publishable_key="pk_test_abc123",
            stripe_webhook_secret="whsec_test",
            session_factory=_SessionFactoryStub(),
        )
        base.update(overrides)
        return base

    def test_stripe_with_full_test_creds_returns_stripe_adapter(self):
        adapter = select_tokenizer(**self._kwargs())
        assert isinstance(adapter, StripeTokenizerAdapter)

    def test_stripe_with_full_live_creds_in_production_returns_stripe_adapter(self):
        adapter = select_tokenizer(
            **self._kwargs(
                env="production",
                stripe_api_key="sk_live_xyz",
                stripe_publishable_key="pk_live_xyz",
            )
        )
        assert isinstance(adapter, StripeTokenizerAdapter)


class TestStripeMissingCredsRefused:
    def test_missing_api_key(self):
        with pytest.raises(RuntimeError, match="BSS_PAYMENT_STRIPE_API_KEY"):
            select_tokenizer(
                name="stripe",
                env="development",
                stripe_publishable_key="pk_test_x",
                stripe_webhook_secret="whsec_x",
                session_factory=_SessionFactoryStub(),
            )

    def test_missing_publishable_key(self):
        with pytest.raises(RuntimeError, match="BSS_PAYMENT_STRIPE_PUBLISHABLE_KEY"):
            select_tokenizer(
                name="stripe",
                env="development",
                stripe_api_key="sk_test_x",
                stripe_webhook_secret="whsec_x",
                session_factory=_SessionFactoryStub(),
            )

    def test_missing_webhook_secret(self):
        # Critical: missing webhook secret means the receiver silently
        # 401s every Stripe delivery; this guard is non-negotiable.
        with pytest.raises(
            RuntimeError, match="BSS_PAYMENT_STRIPE_WEBHOOK_SECRET"
        ):
            select_tokenizer(
                name="stripe",
                env="development",
                stripe_api_key="sk_test_x",
                stripe_publishable_key="pk_test_x",
                session_factory=_SessionFactoryStub(),
            )


class TestStripeKeyModeGuards:
    def test_test_secret_in_production_refused(self):
        with pytest.raises(
            RuntimeError, match=r"sk_test_\* refused in BSS_ENV=production"
        ):
            select_tokenizer(
                name="stripe",
                env="production",
                stripe_api_key="sk_test_xyz",
                stripe_publishable_key="pk_test_xyz",
                stripe_webhook_secret="whsec_x",
                session_factory=_SessionFactoryStub(),
            )

    def test_secret_publishable_mode_mismatch_refused(self):
        with pytest.raises(RuntimeError, match="key mode mismatch"):
            select_tokenizer(
                name="stripe",
                env="development",
                stripe_api_key="sk_test_xyz",
                stripe_publishable_key="pk_live_xyz",
                stripe_webhook_secret="whsec_x",
                session_factory=_SessionFactoryStub(),
            )

    def test_unknown_secret_prefix_refused(self):
        with pytest.raises(RuntimeError, match="must start with sk_test_ or sk_live_"):
            select_tokenizer(
                name="stripe",
                env="development",
                stripe_api_key="abc",
                stripe_publishable_key="pk_test_xyz",
                stripe_webhook_secret="whsec_x",
                session_factory=_SessionFactoryStub(),
            )


class TestAllowTestCardReuseGuards:
    def test_allow_test_card_reuse_with_test_keys_ok(self):
        adapter = select_tokenizer(
            name="stripe",
            env="development",
            stripe_api_key="sk_test_x",
            stripe_publishable_key="pk_test_x",
            stripe_webhook_secret="whsec_x",
            allow_test_card_reuse=True,
            session_factory=_SessionFactoryStub(),
        )
        assert isinstance(adapter, StripeTokenizerAdapter)
        assert adapter._cfg.allow_test_card_reuse is True

    def test_allow_test_card_reuse_with_live_keys_refused(self):
        with pytest.raises(
            RuntimeError, match="ALLOW_TEST_CARD_REUSE.*refused with sk_live_"
        ):
            select_tokenizer(
                name="stripe",
                env="production",
                stripe_api_key="sk_live_x",
                stripe_publishable_key="pk_live_x",
                stripe_webhook_secret="whsec_x",
                allow_test_card_reuse=True,
                session_factory=_SessionFactoryStub(),
            )


class TestSessionFactoryRequiredForStripe:
    def test_stripe_without_session_factory_refused(self):
        with pytest.raises(RuntimeError, match="DB session factory"):
            select_tokenizer(
                name="stripe",
                env="development",
                stripe_api_key="sk_test_x",
                stripe_publishable_key="pk_test_x",
                stripe_webhook_secret="whsec_x",
                session_factory=None,
            )

    def test_mock_does_not_require_session_factory(self):
        adapter = select_tokenizer(name="mock", env="development", session_factory=None)
        assert isinstance(adapter, MockTokenizerAdapter)


class TestUnknownProvider:
    def test_unknown_provider_refused(self):
        with pytest.raises(RuntimeError, match="Unknown BSS_PAYMENT_PROVIDER"):
            select_tokenizer(name="adyen", env="development")
