"""v0.7 — schedule_plan_change + cancel + renewal-time application."""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

# ── helpers ────────────────────────────────────────────────────────────


async def _create_active(client) -> str:
    resp = await client.post(
        "/subscription-api/v1/subscription",
        json={
            "customerId": "CUST-0001",
            "offeringId": "PLAN_M",
            "msisdn": "90000042",
            "iccid": "8910000000000042",
            "paymentMethodId": "PM-0001",
            "priceSnapshot": {
                "priceAmount": "25.00",
                "priceCurrency": "SGD",
                "priceOfferingPriceId": "PRICE_PLAN_M",
            },
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ── schedule_plan_change ───────────────────────────────────────────────


class TestSchedulePlanChange:
    @pytest.mark.asyncio
    async def test_writes_pending_fields_and_emits_event(self, client, mock_clients):
        sub_id = await _create_active(client)
        resp = await client.post(
            f"/subscription-api/v1/subscription/{sub_id}/schedule-plan-change",
            json={"newOfferingId": "PLAN_L"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["pendingOfferingId"] == "PLAN_L"
        assert body["pendingOfferingPriceId"] == "PRICE_PLAN_L"
        assert body["pendingEffectiveAt"] is not None
        # Catalog was queried for the active price at scheduling time.
        mock_clients["catalog"].get_active_price.assert_awaited_with("PLAN_L")

    @pytest.mark.asyncio
    async def test_rejects_same_offering(self, client):
        sub_id = await _create_active(client)
        resp = await client.post(
            f"/subscription-api/v1/subscription/{sub_id}/schedule-plan-change",
            json={"newOfferingId": "PLAN_M"},
        )
        assert resp.status_code == 422
        assert resp.json()["reason"] == "subscription.plan_change.same_offering"

    @pytest.mark.asyncio
    async def test_rejects_target_not_sellable_now(self, client, mock_clients):
        sub_id = await _create_active(client)
        # PLAN_L not in active offerings.
        mock_clients["catalog"].list_active_offerings = AsyncMock(return_value=[
            {"id": "PLAN_S"},
            {"id": "PLAN_M"},
        ])
        resp = await client.post(
            f"/subscription-api/v1/subscription/{sub_id}/schedule-plan-change",
            json={"newOfferingId": "PLAN_L"},
        )
        assert resp.status_code == 422
        assert (
            resp.json()["reason"]
            == "subscription.plan_change.target_not_sellable_now"
        )

    @pytest.mark.asyncio
    async def test_rejects_when_pending_change_already_exists(self, client):
        sub_id = await _create_active(client)
        first = await client.post(
            f"/subscription-api/v1/subscription/{sub_id}/schedule-plan-change",
            json={"newOfferingId": "PLAN_L"},
        )
        assert first.status_code == 200

        second = await client.post(
            f"/subscription-api/v1/subscription/{sub_id}/schedule-plan-change",
            json={"newOfferingId": "PLAN_S"},
        )
        assert second.status_code == 422
        assert (
            second.json()["reason"]
            == "subscription.plan_change.already_pending"
        )

    @pytest.mark.asyncio
    async def test_rejects_terminated_subscription(self, client):
        sub_id = await _create_active(client)
        await client.post(f"/subscription-api/v1/subscription/{sub_id}/terminate")

        resp = await client.post(
            f"/subscription-api/v1/subscription/{sub_id}/schedule-plan-change",
            json={"newOfferingId": "PLAN_L"},
        )
        assert resp.status_code == 422
        assert resp.json()["reason"] == "subscription.plan_change.not_eligible_state"


# ── cancel_pending_plan_change ─────────────────────────────────────────


class TestCancelPlanChange:
    @pytest.mark.asyncio
    async def test_clears_pending_fields(self, client):
        sub_id = await _create_active(client)
        await client.post(
            f"/subscription-api/v1/subscription/{sub_id}/schedule-plan-change",
            json={"newOfferingId": "PLAN_L"},
        )
        resp = await client.post(
            f"/subscription-api/v1/subscription/{sub_id}/cancel-plan-change"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["pendingOfferingId"] is None
        assert body["pendingOfferingPriceId"] is None
        assert body["pendingEffectiveAt"] is None

    @pytest.mark.asyncio
    async def test_idempotent_on_no_pending(self, client):
        sub_id = await _create_active(client)
        resp = await client.post(
            f"/subscription-api/v1/subscription/{sub_id}/cancel-plan-change"
        )
        assert resp.status_code == 200


# ── renewal-time application ───────────────────────────────────────────


class TestRenewalApplication:
    @pytest.mark.asyncio
    async def test_pending_in_future_does_not_apply_at_renewal(
        self, client, mock_clients
    ):
        """Pending effective in the future → renewal stays on current plan."""
        sub_id = await _create_active(client)

        # Schedule first — pending_effective_at is set to next_renewal_at,
        # which is ~30 days from now in our test fixture.
        await client.post(
            f"/subscription-api/v1/subscription/{sub_id}/schedule-plan-change",
            json={"newOfferingId": "PLAN_L"},
        )

        # Manually renew now (before pending_effective_at) — old plan still.
        mock_clients["payment"].charge.reset_mock()
        resp = await client.post(f"/subscription-api/v1/subscription/{sub_id}/renew")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Still PLAN_M at the snapshot price.
        assert body["offeringId"] == "PLAN_M"
        assert body["pendingOfferingId"] == "PLAN_L"
        # Charged $25 (the snapshot), not $45 (PLAN_L).
        charge_args = mock_clients["payment"].charge.await_args
        assert charge_args.kwargs["amount"] == Decimal("25.00")

    @pytest.mark.asyncio
    async def test_pending_due_now_applies_at_renewal(self, client, mock_clients):
        """Backdate pending_effective_at, run renew → swap takes effect."""
        from bss_models.subscription import Subscription
        from sqlalchemy import update

        sub_id = await _create_active(client)
        await client.post(
            f"/subscription-api/v1/subscription/{sub_id}/schedule-plan-change",
            json={"newOfferingId": "PLAN_L"},
        )

        # Force pending_effective_at into the past.
        db_session = client._transport.app.state.session_factory()._session  # type: ignore[attr-defined]
        await db_session.execute(
            update(Subscription)
            .where(Subscription.id == sub_id)
            .values(pending_effective_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
        )
        await db_session.flush()

        # Catalog returns PLAN_L's bundle allowances on get_offering for the new plan.
        mock_clients["catalog"].get_offering = AsyncMock(return_value={
            "id": "PLAN_L",
            "name": "Max",
            "productOfferingPrice": [{
                "id": "PRICE_PLAN_L",
                "priceType": "recurring",
                "price": {"taxIncludedAmount": {"value": "45.00", "unit": "SGD"}},
            }],
            "bundleAllowance": [
                {"allowanceType": "data", "quantity": 153600, "unit": "mb"},
                {"allowanceType": "voice", "quantity": -1, "unit": "minutes"},
                {"allowanceType": "sms", "quantity": -1, "unit": "count"},
            ],
        })
        mock_clients["payment"].charge.reset_mock()

        resp = await client.post(f"/subscription-api/v1/subscription/{sub_id}/renew")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["offeringId"] == "PLAN_L"
        assert body["pendingOfferingId"] is None
        assert body["priceAmount"] == "45.00"
        # Charged the new price.
        charge_args = mock_clients["payment"].charge.await_args
        assert charge_args.kwargs["amount"] == Decimal("45.00")

    @pytest.mark.asyncio
    async def test_payment_failure_keeps_pending_intact(self, client, mock_clients):
        """If the renewal charge declines, plan-change stays scheduled."""
        from bss_models.subscription import Subscription
        from sqlalchemy import update

        sub_id = await _create_active(client)
        await client.post(
            f"/subscription-api/v1/subscription/{sub_id}/schedule-plan-change",
            json={"newOfferingId": "PLAN_L"},
        )
        db_session = client._transport.app.state.session_factory()._session  # type: ignore[attr-defined]
        await db_session.execute(
            update(Subscription)
            .where(Subscription.id == sub_id)
            .values(pending_effective_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
        )
        await db_session.flush()

        mock_clients["payment"].charge = AsyncMock(return_value={
            "id": "PAY-DECLINED",
            "status": "declined",
            "declineReason": "insufficient_funds",
        })

        resp = await client.post(f"/subscription-api/v1/subscription/{sub_id}/renew")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["state"] == "blocked"
        # Old plan still. Pending fields preserved.
        assert body["offeringId"] == "PLAN_M"
        assert body["pendingOfferingId"] == "PLAN_L"
