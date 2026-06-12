"""Stripe webhook receiver tests (v0.16 Track 3).

Drives the receiver against the Track 0 redacted fixtures
(``services/payment/tests/fixtures/webhook_*.json``) which are pre-signed
with ``whsec_test_fixture_v0160`` over a fixed timestamp ``1700000000``.

The fixture-test-secret-and-fixed-timestamp combination is the v0.15
doctrine carried into Track 3: Track 0 captured real Stripe deliveries,
verified the signature scheme matches Stripe's wire format end-to-end,
then redacted + re-signed so the test suite is deterministic and
secret-free.
"""

from __future__ import annotations

import json
import pathlib

import pytest
import pytest_asyncio
from bss_models import PaymentAttempt
from bss_models.audit import DomainEvent
from bss_models.integrations import WebhookEvent
from sqlalchemy import select

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
FIXTURE_WHSEC = "whsec_test_fixture_v0160"
FIXTURE_TS = 1700000000


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# Pin time before each test so the fixture's stale signature
# (timestamp=1700000000, well in the past) doesn't trip the verifier's
# 5-minute freshness skew. We monkeypatch `time.time` for the verify
# call and rely on max_skew_seconds default (300) by replacing the
# settings.payment_stripe_webhook_secret with the fixture secret.


@pytest_asyncio.fixture
async def stripe_client(client, monkeypatch):
    """Configure the test app to verify webhooks against the fixture secret.

    Also pins ``time.time`` to FIXTURE_TS so the verifier's freshness
    skew check passes — the fixtures were signed in 2023 but the test
    must verify them today, so we time-travel for the duration.
    """
    # 1. Plant the fixture secret onto settings so the receiver picks it up.
    settings = client._transport.app.state.settings
    monkeypatch.setattr(
        settings, "payment_stripe_webhook_secret", FIXTURE_WHSEC, raising=False
    )

    # 2. Pin time inside the verifier (it imports `time.time`).
    import bss_webhooks.signatures as sigmod

    monkeypatch.setattr(sigmod.time, "time", lambda: FIXTURE_TS + 1)

    return client


class TestSignatureGuard:
    @pytest.mark.asyncio
    async def test_tampered_signature_returns_401(
        self, stripe_client, db_session
    ):
        fx = _load_fixture("webhook_charge_succeeded.json")
        body_bytes = fx["body_raw"].encode("utf-8")
        event_id = fx["body_parsed"]["id"]
        bad_headers = dict(fx["headers"])
        # Flip one hex char in the v1 sig — guaranteed mismatch.
        sig = bad_headers["Stripe-Signature"]
        bad_headers["Stripe-Signature"] = sig[:-1] + (
            "0" if sig[-1] != "0" else "1"
        )
        resp = await stripe_client.post(
            "/webhooks/stripe", content=body_bytes, headers=bad_headers
        )
        assert resp.status_code == 401
        assert b"signature_mismatch" in resp.content

        # No webhook_event row written on rejection. (Scope to THIS
        # event_id; the table may have stale rows from prior hero
        # scenario runs that committed normally.)
        result = await db_session.execute(
            select(WebhookEvent).where(
                WebhookEvent.provider == "stripe",
                WebhookEvent.event_id == event_id,
            )
        )
        assert result.scalars().first() is None

    @pytest.mark.asyncio
    async def test_signature_invalid_emits_diagnostic_log(
        self, stripe_client, capsys
    ):
        # structlog's default config writes JSON to stdout; capsys reads
        # it directly. caplog only captures the standard logging library,
        # which structlog bypasses, so structlog events don't appear there.
        fx = _load_fixture("webhook_charge_succeeded.json")
        body_bytes = fx["body_raw"].encode("utf-8")
        bad_headers = dict(fx["headers"])
        bad_headers["Stripe-Signature"] = "t=1700000000,v1=deadbeef"

        await stripe_client.post(
            "/webhooks/stripe", content=body_bytes, headers=bad_headers
        )
        captured = capsys.readouterr().out

        # v0.16 day-1 doctrine: every signature_invalid log must
        # include candidate_headers AND body_preview so silent 401s
        # are never un-debuggable.
        assert "payment.webhook.signature_invalid" in captured
        assert "candidate_headers" in captured
        assert "body_preview" in captured
        # Webhook secret must NEVER appear in the diagnostic log.
        assert "whsec_test_fixture_v0160" not in captured

    @pytest.mark.asyncio
    async def test_no_secret_returns_401_misconfigured(self, client, monkeypatch):
        # Receiver runs in a payment-mode=mock deployment that has no
        # webhook secret. Behavior: 401 with diagnostic log, never crash.
        settings = client._transport.app.state.settings
        monkeypatch.setattr(
            settings, "payment_stripe_webhook_secret", "", raising=False
        )
        fx = _load_fixture("webhook_charge_succeeded.json")
        resp = await client.post(
            "/webhooks/stripe",
            content=fx["body_raw"].encode("utf-8"),
            headers=fx["headers"],
        )
        assert resp.status_code == 401
        assert b"webhook_secret_unset" in resp.content


class TestEventRouting:
    @pytest.mark.asyncio
    async def test_charge_succeeded_persists_webhook_event(
        self, stripe_client, db_session
    ):
        fx = _load_fixture("webhook_charge_succeeded.json")
        resp = await stripe_client.post(
            "/webhooks/stripe",
            content=fx["body_raw"].encode("utf-8"),
            headers=fx["headers"],
        )
        assert resp.status_code == 200, resp.content

        # The webhook row is committed in the receiver's own session,
        # which the test session_factory shares with the test session.
        result = await db_session.execute(
            select(WebhookEvent).where(WebhookEvent.provider == "stripe")
        )
        rows = result.scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.event_type == "charge.succeeded"
        assert row.signature_valid is True
        assert row.process_outcome in ("reconciled", "noop")  # noop if no attempt row

    @pytest.mark.asyncio
    async def test_duplicate_event_id_returns_deduped(
        self, stripe_client, db_session
    ):
        fx = _load_fixture("webhook_charge_succeeded.json")
        body_bytes = fx["body_raw"].encode("utf-8")
        # First delivery
        r1 = await stripe_client.post(
            "/webhooks/stripe", content=body_bytes, headers=fx["headers"]
        )
        assert r1.status_code == 200
        # Provider retry — same event_id
        r2 = await stripe_client.post(
            "/webhooks/stripe", content=body_bytes, headers=fx["headers"]
        )
        assert r2.status_code == 200
        assert b"deduped" in r2.content

        # Still exactly one row.
        result = await db_session.execute(
            select(WebhookEvent).where(WebhookEvent.provider == "stripe")
        )
        assert len(result.scalars().all()) == 1

    @pytest.mark.asyncio
    async def test_refund_emits_payment_refunded_domain_event(
        self, stripe_client, db_session
    ):
        fx = _load_fixture("webhook_charge_refunded.json")
        resp = await stripe_client.post(
            "/webhooks/stripe",
            content=fx["body_raw"].encode("utf-8"),
            headers=fx["headers"],
        )
        assert resp.status_code == 200, resp.content

        # Scope to JUST-emitted events; fixture is deterministic so
        # prior manual-smoke runs may have committed identical-payload
        # events.
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=10)
        pi_id = fx["body_parsed"]["data"]["object"]["payment_intent"]
        result = await db_session.execute(
            select(DomainEvent).where(
                DomainEvent.event_type == "payment.refunded",
                DomainEvent.occurred_at >= cutoff,
            )
        )
        events = result.scalars().all()
        ours = [
            e for e in events if e.payload.get("provider_call_id") == pi_id
        ]
        assert len(ours) == 1
        ev = ours[0]
        # Motto #1: amount is recorded, NOT auto-applied to a balance.
        assert "amount_refunded_minor" in ev.payload
        assert "provider_call_id" in ev.payload

    @pytest.mark.asyncio
    async def test_dispute_emits_payment_dispute_opened_domain_event(
        self, stripe_client, db_session
    ):
        # Baseline counts of case.* / service.blocked from prior hero
        # scenario runs that committed normally (and aren't rolled back
        # by this test's transaction). The motto-#1 assertion is "this
        # webhook doesn't ADD any case.opened or service.blocked rows",
        # not "the table is empty."
        from sqlalchemy import func
        baseline_q = select(func.count(DomainEvent.event_id)).where(
            DomainEvent.event_type.in_(["case.opened", "service.blocked"])
        )
        baseline_count = (await db_session.execute(baseline_q)).scalar_one()

        fx = _load_fixture("webhook_charge_dispute_created.json")
        resp = await stripe_client.post(
            "/webhooks/stripe",
            content=fx["body_raw"].encode("utf-8"),
            headers=fx["headers"],
        )
        assert resp.status_code == 200, resp.content

        # Find OUR event by occurred_at (just-emitted, within the test's
        # narrow window). The fixture's stripe_dispute_id is deterministic
        # so prior manual-smoke runs may have committed events with the
        # same id; counting by id over-counts.
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=10)
        result = await db_session.execute(
            select(DomainEvent).where(
                DomainEvent.event_type == "payment.dispute_opened",
                DomainEvent.occurred_at >= cutoff,
            )
        )
        events = result.scalars().all()
        dispute_id = fx["body_parsed"]["data"]["object"]["id"]
        ours = [
            e for e in events if e.payload.get("stripe_dispute_id") == dispute_id
        ]
        assert len(ours) == 1
        ev = ours[0]
        assert ev.payload["stripe_dispute_id"] == dispute_id

        # Motto #1: NO new case.opened, NO new service.blocked rows
        # added by this webhook delivery. The cockpit surfaces the
        # dispute event; the operator opens a case if they choose.
        after_count = (await db_session.execute(baseline_q)).scalar_one()
        assert after_count == baseline_count


class TestDriftDetection:
    @pytest.mark.asyncio
    async def test_webhook_says_succeeded_but_row_says_declined_emits_drift(
        self, stripe_client, db_session
    ):
        # Plant a row that contradicts the webhook fixture.
        fx = _load_fixture("webhook_charge_succeeded.json")
        # The fixture's data.object.payment_intent
        pi_id = fx["body_parsed"]["data"]["object"]["payment_intent"]

        # Need a payment_method first (FK).
        from bss_models import PaymentMethod
        pm = PaymentMethod(
            id="PM-DRIFT-001",
            customer_id="CUST-DRIFT-001",
            type="card",
            token="tok_drift",
            token_provider="stripe",
            last4="4242",
            brand="visa",
            exp_month=12,
            exp_year=2030,
            is_default=True,
            status="active",
            tenant_id="DEFAULT",
        )
        db_session.add(pm)
        await db_session.flush()

        from datetime import datetime, timezone
        from decimal import Decimal
        attempt = PaymentAttempt(
            id="ATT-DRIFT-001",
            customer_id="CUST-DRIFT-001",
            payment_method_id="PM-DRIFT-001",
            amount=Decimal("10.00"),
            currency="SGD",
            purpose="test",
            status="declined",          # contradicts webhook
            gateway_ref=pi_id,
            decline_reason="card_declined",
            provider_call_id=pi_id,    # match the webhook
            decline_code="card_declined",
            attempted_at=datetime.now(timezone.utc),
            tenant_id="DEFAULT",
        )
        db_session.add(attempt)
        await db_session.flush()

        resp = await stripe_client.post(
            "/webhooks/stripe",
            content=fx["body_raw"].encode("utf-8"),
            headers=fx["headers"],
        )
        assert resp.status_code == 200

        # Row state must NOT have been overwritten; sync result is canonical.
        await db_session.refresh(attempt)
        assert attempt.status == "declined"

        # A drift event was emitted.
        result = await db_session.execute(
            select(DomainEvent).where(
                DomainEvent.event_type == "payment.attempt_state_drift"
            )
        )
        drifts = result.scalars().all()
        assert len(drifts) == 1
        assert drifts[0].payload["row_status"] == "declined"
        assert drifts[0].payload["webhook_status"] == "approved"
