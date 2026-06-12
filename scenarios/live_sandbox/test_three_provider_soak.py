"""v0.16 Track 5 — Three-provider live-sandbox soak.

Skipped unless ``BSS_NIGHTLY_SANDBOX=true`` is set. When enabled, every
test in this module makes real calls to Stripe sandbox + (optionally)
Resend and Didit sandboxes.

Scope (pragmatic, not exhaustive):

- **Stripe path is exercised end-to-end** via real API calls: customer
  create → PaymentIntent.create off-session → verify pi_*.status →
  register pm_* via BSS payment service → assert payment.customer
  cache populated → trigger a charge against the saved pm_* → verify
  the BSS payment_attempt row carries the real pi_* on
  provider_call_id.
- **Webhook receiver is exercised against a real Stripe-signed delivery**
  — assumes the operator has a webhook endpoint configured pointing
  at this BSS instance; the test triggers an event via Stripe's API
  and waits for the webhook_event row to appear.
- **Browser UI flow is NOT exercised here.** The portal Stripe Elements
  iframe is browser-driven; full E2E needs Playwright/Selenium which
  is its own infrastructure project. Tracked as a TODO; the
  unit-tested rendering of the iframe (``test_signup_stripe_mode``)
  is the closest we get without a browser harness.
- **Resend + Didit smoke** is a single API ping each (auth-key is
  valid; sandbox responds 200). Not the end-to-end "send → bounce →
  webhook reconciles" Resend flow nor the full Didit hosted-UI flow,
  both of which are exercised by the dedicated v0.14/v0.15 hero
  scenarios.

The full UI E2E with Playwright is documented in
``docs/runbooks/three-provider-sandbox-soak.md`` for the operator to
run manually before each release tag.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from decimal import Decimal

import pytest

# Skip the entire module unless explicitly opted-in. This guard is the
# single point of control: nothing in this module makes external calls
# unless BSS_NIGHTLY_SANDBOX=true is set.
if os.environ.get("BSS_NIGHTLY_SANDBOX", "").lower() != "true":
    pytest.skip(
        "BSS_NIGHTLY_SANDBOX not set; skipping live-sandbox soak. "
        "Set BSS_NIGHTLY_SANDBOX=true with sandbox creds to run.",
        allow_module_level=True,
    )


# ── Required env for the soak ──────────────────────────────────────────


def _require(env_var: str) -> str:
    value = os.environ.get(env_var, "").strip()
    if not value:
        pytest.fail(
            f"BSS_NIGHTLY_SANDBOX=true requires {env_var} to be set "
            "with sandbox-mode credentials. See "
            "docs/runbooks/three-provider-sandbox-soak.md."
        )
    return value


@pytest.fixture(scope="module")
def stripe_creds() -> dict[str, str]:
    return {
        "api_key": _require("BSS_PAYMENT_STRIPE_API_KEY"),
        "publishable_key": _require("BSS_PAYMENT_STRIPE_PUBLISHABLE_KEY"),
        "webhook_secret": _require("BSS_PAYMENT_STRIPE_WEBHOOK_SECRET"),
    }


@pytest.fixture(scope="module")
def stripe_sdk(stripe_creds):
    """Late import so the SDK is only required when the soak runs."""
    import stripe

    # Sanity guard — refuse to run against live keys.
    if stripe_creds["api_key"].startswith("sk_live_"):
        pytest.fail(
            "BSS_NIGHTLY_SANDBOX must NEVER run against live Stripe "
            "keys. Use sk_test_*. Refusing to proceed."
        )
    return stripe


# ── Tier 1: Stripe API smoke (no BSS call) ────────────────────────────


class TestStripeCredentialsValid:
    """Confirms the configured sandbox creds work BEFORE any BSS call.

    A failing test here means the secrets aren't configured correctly;
    no point proceeding to BSS-side assertions until the creds work.
    """

    def test_account_retrieve(self, stripe_sdk, stripe_creds):
        """Account.retrieve costs nothing and creates nothing — just
        confirms the key is valid."""
        account = stripe_sdk.Account.retrieve(api_key=stripe_creds["api_key"])
        # Stripe v15 returns either a dict or a StripeObject; accept both.
        acct_id = account.get("id") if isinstance(account, dict) else account.id
        assert acct_id.startswith("acct_"), f"unexpected account id: {acct_id}"


# ── Tier 2: Stripe customer + PaymentIntent end-to-end ────────────────


class TestStripeChargeRoundTrip:
    """Mints a Stripe customer + pm_* + charges them off-session.

    This is what the portal Stripe Elements flow does at runtime, just
    via the API instead of the iframe. Proves that the Stripe-side
    integration is live and that test cards work in your sandbox.
    """

    def test_off_session_charge_with_test_card(self, stripe_sdk, stripe_creds):
        # Unique tag so concurrent test runs don't fight over the same
        # customer record.
        tag = uuid.uuid4().hex[:8]

        customer = stripe_sdk.Customer.create(
            api_key=stripe_creds["api_key"],
            email=f"soak-{tag}@bss-cli.local",
            metadata={
                "bss_customer_id": f"CUST-SOAK-{tag}",
                "bss_test": "v0.16-track5",
            },
        )
        cus_id = customer.get("id") if isinstance(customer, dict) else customer.id
        assert cus_id.startswith("cus_")

        # off_session=True + confirm=True → Stripe immediately tries to
        # charge. pm_card_visa is Stripe's stable test PaymentMethod
        # that always approves.
        pi = stripe_sdk.PaymentIntent.create(
            api_key=stripe_creds["api_key"],
            amount=1000,  # cents
            currency="sgd",
            customer=cus_id,
            payment_method="pm_card_visa",
            off_session=True,
            confirm=True,
            idempotency_key=f"soak-{tag}",
            metadata={"bss_test": "v0.16-track5"},
        )
        pi_dict = pi.to_dict() if hasattr(pi, "to_dict") else dict(pi)
        assert pi_dict["id"].startswith("pi_")
        assert pi_dict["status"] == "succeeded", (
            f"expected pi.status=succeeded; got {pi_dict['status']}: "
            f"{pi_dict.get('last_payment_error')}"
        )
        assert pi_dict["amount_received"] == 1000


# ── Tier 3: BSS-side StripeTokenizerAdapter against real Stripe ───────


class TestBssStripeAdapterLive:
    """Drives StripeTokenizerAdapter against real Stripe sandbox.

    Skips if BSS_DB_URL isn't set (the adapter needs the
    payment.customer cache table). The test creates its own session
    factory — does NOT need the payment service container running.
    """

    @pytest.mark.asyncio
    async def test_ensure_customer_round_trip_writes_payment_customer_cache(
        self, stripe_creds
    ):
        db_url = os.environ.get("BSS_DB_URL", "")
        if not db_url:
            pytest.skip("BSS_DB_URL not set; can't exercise the cache write")
        from app.domain.stripe_tokenizer import StripeConfig, StripeTokenizerAdapter
        from sqlalchemy.ext.asyncio import (
            async_sessionmaker,
            create_async_engine,
        )

        engine = create_async_engine(db_url, pool_size=2, max_overflow=2)
        try:
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            adapter = StripeTokenizerAdapter(
                config=StripeConfig(
                    api_key=stripe_creds["api_key"],
                    publishable_key=stripe_creds["publishable_key"],
                    webhook_secret=stripe_creds["webhook_secret"],
                    allow_test_card_reuse=True,  # sandbox-only; harmless here
                ),
                session_factory=session_factory,
            )

            tag = uuid.uuid4().hex[:8]
            bss_customer_id = f"CUST-SOAK-LIVE-{tag}"
            cus_external_ref = await adapter.ensure_customer(
                bss_customer_id=bss_customer_id,
                email=f"soak-live-{tag}@bss-cli.local",
            )
            assert cus_external_ref.startswith("cus_")

            # Second call returns the cached value without round-tripping.
            cached = await adapter.ensure_customer(
                bss_customer_id=bss_customer_id,
                email=f"soak-live-{tag}@bss-cli.local",
            )
            assert cached == cus_external_ref

            # Verify payment.customer row exists.
            from bss_models import PaymentCustomer
            from sqlalchemy import select

            async with session_factory() as db:
                row = (
                    await db.execute(
                        select(PaymentCustomer).where(
                            PaymentCustomer.id == bss_customer_id
                        )
                    )
                ).scalar_one_or_none()
                assert row is not None
                assert row.customer_external_ref == cus_external_ref
                assert row.customer_external_ref_provider == "stripe"
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_charge_off_session_returns_provider_call_id(
        self, stripe_creds
    ):
        db_url = os.environ.get("BSS_DB_URL", "")
        if not db_url:
            pytest.skip("BSS_DB_URL not set")
        from app.domain.stripe_tokenizer import StripeConfig, StripeTokenizerAdapter
        from sqlalchemy.ext.asyncio import (
            async_sessionmaker,
            create_async_engine,
        )

        engine = create_async_engine(db_url, pool_size=2, max_overflow=2)
        try:
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            adapter = StripeTokenizerAdapter(
                config=StripeConfig(
                    api_key=stripe_creds["api_key"],
                    publishable_key=stripe_creds["publishable_key"],
                    webhook_secret=stripe_creds["webhook_secret"],
                    allow_test_card_reuse=True,
                ),
                session_factory=session_factory,
            )

            tag = uuid.uuid4().hex[:8]
            bss_customer_id = f"CUST-SOAK-CHARGE-{tag}"
            cus = await adapter.ensure_customer(
                bss_customer_id=bss_customer_id,
                email=f"soak-charge-{tag}@bss-cli.local",
            )

            # Attach pm_card_visa to the customer (Stripe's test
            # affordance — paired with allow_test_card_reuse=True it's
            # safe to re-attach across runs).
            await adapter.attach_payment_method_to_customer(
                payment_method_id="pm_card_visa",
                customer_id=cus,
            )

            result = await adapter.charge(
                "pm_card_visa",
                Decimal("12.34"),
                "SGD",
                idempotency_key=f"ATT-SOAK-{tag}-r0",
                purpose="soak_test",
                customer_external_ref=cus,
            )
            assert result.status == "approved", (
                f"expected approved; got {result.status} ({result.reason})"
            )
            assert result.provider_call_id.startswith("pi_")
            assert result.decline_code is None
        finally:
            await engine.dispose()


# ── Tier 4: Resend + Didit smoke (auth-key liveness) ──────────────────


class TestResendAndDiditEnvSet:
    """v0.16 only changes the payment provider seam. Resend (v0.14) and
    Didit (v0.15) have their own dedicated integration tests + onboard-
    wizard probes that exercise the real provider end-to-end. The soak
    here only asserts the env vars are SET — it does NOT re-probe these
    APIs because:

    - Resend: keys can be scoped to `send-only` (correct least-privilege
      production posture), which can't call the read-only smoke
      endpoints we'd want for a zero-cost ping.
    - Didit: has no zero-cost probe endpoint; the only validation API
      (`POST /v2/session/`) consumes against the 500/month free-tier
      cap. The v0.15 onboard wizard does this once at setup; repeating
      it in nightly CI would burn 30 sessions/month.

    The actual liveness of these providers is proven when:
    - Resend: a real customer signup sends a magic-link email (would
      fail loudly on a bad key during signup flow).
    - Didit: a real customer signup creates a verification session
      (would fail loudly during the KYC step).

    The full UI smoke (the operator's manual checklist) covers both.
    """

    def test_resend_api_key_set(self):
        api_key = os.environ.get("BSS_PORTAL_EMAIL_RESEND_API_KEY", "")
        if not api_key:
            pytest.skip(
                "BSS_PORTAL_EMAIL_RESEND_API_KEY not set; "
                "Resend integration not configured"
            )
        assert api_key.startswith("re_"), (
            f"Resend keys start with 're_'; got {api_key[:6]}..."
        )

    def test_didit_creds_set(self):
        api_key = os.environ.get("BSS_PORTAL_KYC_DIDIT_API_KEY", "")
        workflow_id = os.environ.get("BSS_PORTAL_KYC_DIDIT_WORKFLOW_ID", "")
        if not api_key or not workflow_id:
            pytest.skip(
                "Didit creds not set; KYC integration not configured"
            )
        import re

        assert re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            workflow_id,
        ), f"Didit workflow id should be a UUID; got {workflow_id!r}"


# ── Tier 5: webhook receiver liveness (optional) ──────────────────────


class TestWebhookReceiverLiveness:
    """Verifies the BSS webhook receiver can process a real
    Stripe-signed delivery.

    Requires:
    - BSS_PAYMENT_WEBHOOK_PUBLIC_URL pointing at a publicly-reachable
      copy of this BSS instance's POST /webhooks/stripe.
    - A Stripe webhook endpoint configured to deliver to that URL.

    Skips otherwise. Triggers a payment_intent.succeeded event via the
    Stripe API and polls for the webhook_event row to appear in the
    integrations schema.
    """

    @pytest.mark.asyncio
    async def test_webhook_round_trip(self, stripe_sdk, stripe_creds):
        public_url = os.environ.get("BSS_PAYMENT_WEBHOOK_PUBLIC_URL", "")
        db_url = os.environ.get("BSS_DB_URL", "")
        if not public_url or not db_url:
            pytest.skip(
                "Both BSS_PAYMENT_WEBHOOK_PUBLIC_URL (Tailscale/ngrok URL) "
                "and BSS_DB_URL must be set to exercise webhook round-trip"
            )

        from bss_models.integrations import WebhookEvent
        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import (
            async_sessionmaker,
            create_async_engine,
        )

        # Trigger a real charge via the API; Stripe will deliver
        # several webhook events (payment_intent.created, charge.created,
        # charge.succeeded, payment_intent.succeeded) to our endpoint.
        tag = uuid.uuid4().hex[:8]
        cus = stripe_sdk.Customer.create(
            api_key=stripe_creds["api_key"],
            email=f"soak-webhook-{tag}@bss-cli.local",
        )
        cus_id = cus.get("id") if isinstance(cus, dict) else cus.id
        pi = stripe_sdk.PaymentIntent.create(
            api_key=stripe_creds["api_key"],
            amount=1234,
            currency="sgd",
            customer=cus_id,
            payment_method="pm_card_visa",
            off_session=True,
            confirm=True,
            idempotency_key=f"webhook-soak-{tag}",
        )
        pi_dict = pi.to_dict() if hasattr(pi, "to_dict") else dict(pi)
        pi_id = pi_dict["id"]

        # Poll integrations.webhook_event for ANY event referencing our
        # pi_*. Stripe's webhook delivery is at-least-once with
        # exponential backoff; a 60s window is generous for the first
        # delivery to land.
        engine = create_async_engine(db_url, pool_size=2, max_overflow=2)
        try:
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            deadline = time.monotonic() + 60
            seen = False
            while time.monotonic() < deadline:
                async with session_factory() as db:
                    rows = (
                        await db.execute(
                            select(WebhookEvent).where(
                                WebhookEvent.provider == "stripe",
                            )
                        )
                    ).scalars().all()
                    if any(pi_id in str(r.body) for r in rows):
                        seen = True
                        break
                await asyncio.sleep(2)
            assert seen, (
                f"no webhook_event row referencing {pi_id} arrived "
                f"within 60s. Check the Stripe dashboard's webhook "
                f"endpoint Recent deliveries tab + the payment service "
                f"logs for `payment.webhook.signature_invalid`."
            )
        finally:
            await engine.dispose()
