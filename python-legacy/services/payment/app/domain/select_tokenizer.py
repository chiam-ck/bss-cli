"""Resolve ``BSS_PAYMENT_PROVIDER`` to a concrete ``TokenizerAdapter`` (v0.16).

Mirrors v0.14 ``select_email_provider`` and v0.15 ``select_kyc_adapter``:
fail-fast on misconfig at startup, never silently downgrade. Silent
fallback to mock when Stripe creds are missing is the doctrine bug v0.14
called out — the operator deserves a loud "you said stripe but didn't
configure it" at boot, not a customer-facing "card declined" two weeks
in.

Four guards, all enforced at startup before the FastAPI app accepts
traffic:

1. **Unknown provider name.** Anything other than ``mock`` or ``stripe``
   raises. Adyen / Checkout.com are reserved Protocol shape compatibility
   only; they do not have impls in v0.16.

2. **Stripe + missing credentials.** ``BSS_PAYMENT_PROVIDER=stripe``
   without API key + publishable key + webhook secret raises with the
   specific missing-var name. The webhook-secret guard is non-negotiable
   — if it's missing, the receiver would silently 401 every Stripe
   delivery and reconciliation would never happen.

3. **Production + sk_test_*.** ``BSS_ENV=production`` paired with a test
   secret key raises. Same for the publishable key and the secret/
   publishable mode mismatch (``sk_test_*`` + ``pk_live_*`` or vice
   versa). v0.16 trap #6: ``BSS_ENV`` is a doctrine, not a string.

4. **ALLOW_TEST_CARD_REUSE + sk_live_*.** The sandbox-only test-card
   relink affordance is refused at startup if paired with a live key.
   The flag bypasses Stripe's payment_method_already_attached guard,
   which is fine for ``sk_test_*`` ``pm_card_visa`` (a single test
   card shared across many sandbox customers) but a security disaster
   for real customer cards.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.domain.mock_tokenizer import MockTokenizerAdapter
from app.domain.stripe_tokenizer import StripeConfig, StripeTokenizerAdapter
from app.domain.tokenizer import TokenizerAdapter

if TYPE_CHECKING:
    pass


def select_tokenizer(
    *,
    name: str,
    env: str,
    stripe_api_key: str = "",
    stripe_publishable_key: str = "",
    stripe_webhook_secret: str = "",
    allow_test_card_reuse: bool = False,
    session_factory=None,
) -> TokenizerAdapter:
    """Resolve a ``TokenizerAdapter`` from env config.

    Raises ``RuntimeError`` (or ``ValueError``) on any misconfiguration —
    the FastAPI lifespan calls this and lets the exception propagate, so
    the service refuses to start.
    """
    if name == "mock":
        return MockTokenizerAdapter()

    if name == "stripe":
        if not stripe_api_key:
            raise RuntimeError(
                "BSS_PAYMENT_PROVIDER=stripe requires "
                "BSS_PAYMENT_STRIPE_API_KEY"
            )
        if not stripe_publishable_key:
            raise RuntimeError(
                "BSS_PAYMENT_PROVIDER=stripe requires "
                "BSS_PAYMENT_STRIPE_PUBLISHABLE_KEY"
            )
        if not stripe_webhook_secret:
            raise RuntimeError(
                "BSS_PAYMENT_PROVIDER=stripe requires "
                "BSS_PAYMENT_STRIPE_WEBHOOK_SECRET (without it the "
                "webhook receiver would silently 401 every Stripe "
                "delivery and charge reconciliation would never happen)"
            )

        is_test_secret = stripe_api_key.startswith("sk_test_")
        is_live_secret = stripe_api_key.startswith("sk_live_")
        is_test_publishable = stripe_publishable_key.startswith("pk_test_")
        is_live_publishable = stripe_publishable_key.startswith("pk_live_")

        if not (is_test_secret or is_live_secret):
            raise RuntimeError(
                "BSS_PAYMENT_STRIPE_API_KEY must start with sk_test_ or sk_live_"
            )
        if not (is_test_publishable or is_live_publishable):
            raise RuntimeError(
                "BSS_PAYMENT_STRIPE_PUBLISHABLE_KEY must start with "
                "pk_test_ or pk_live_"
            )

        # Mode mismatch: secret and publishable must agree.
        if is_test_secret != is_test_publishable:
            raise RuntimeError(
                "Stripe key mode mismatch: secret and publishable keys "
                "must both be test (sk_test_/pk_test_) or both be live "
                "(sk_live_/pk_live_); refusing to start with mixed mode"
            )

        if env == "production" and is_test_secret:
            raise RuntimeError(
                "BSS_PAYMENT_STRIPE_API_KEY=sk_test_* refused in "
                "BSS_ENV=production; production must use sk_live_*"
            )

        if allow_test_card_reuse and is_live_secret:
            raise RuntimeError(
                "BSS_PAYMENT_ALLOW_TEST_CARD_REUSE=true is sandbox-only "
                "and refused with sk_live_*; setting it would let one "
                "real customer's payment_method re-attach to a different "
                "real customer (security disaster)"
            )

        if session_factory is None:
            raise RuntimeError(
                "StripeTokenizerAdapter requires a DB session factory "
                "for caching customer_external_ref and writing "
                "integrations.external_call rows"
            )

        return StripeTokenizerAdapter(
            config=StripeConfig(
                api_key=stripe_api_key,
                publishable_key=stripe_publishable_key,
                webhook_secret=stripe_webhook_secret,
                allow_test_card_reuse=allow_test_card_reuse,
            ),
            session_factory=session_factory,
        )

    raise RuntimeError(
        f"Unknown BSS_PAYMENT_PROVIDER={name!r}; expected 'mock' | 'stripe'"
    )
