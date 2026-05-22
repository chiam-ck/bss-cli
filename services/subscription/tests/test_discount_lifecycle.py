"""v1.1 — promo discount on create + renewal counter + plan-change clear.

price_amount stays the FULL base; the effective price is charged each period
while discount_periods_remaining is live. Walks the plan's worked example
($25 base, 20% off, 3 periods → $20 x3 then $25).
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

CREATE = "/subscription-api/v1/subscription"


def _create_body(**snapshot_extra):
    return {
        "customerId": "CUST-0001",
        "offeringId": "PLAN_M",
        "msisdn": "90000042",
        "iccid": "8910000000000042",
        "paymentMethodId": "PM-0001",
        "priceSnapshot": {
            "priceAmount": "25.00",
            "priceCurrency": "SGD",
            "priceOfferingPriceId": "PRICE_PLAN_M",
            **snapshot_extra,
        },
    }


def _last_charge(mock_clients):
    return mock_clients["payment"].charge.await_args.kwargs["amount"]


async def _renew(client, sub_id, mock_clients):
    mock_clients["payment"].charge.reset_mock()
    r = await client.post(f"{CREATE}/{sub_id}/renew")
    assert r.status_code == 200, r.text
    return r.json()


class TestCreateChargesEffective:
    @pytest.mark.asyncio
    async def test_percent_multi_create(self, client, mock_clients):
        r = await client.post(CREATE, json=_create_body(
            discountType="percent", discountValue="20", discountPeriodsTotal=3,
            promoCode="SUMMER", promoOfferDefinitionId="OD_PROMO_SUMMER",
        ))
        assert r.status_code == 201, r.text
        body = r.json()
        # base preserved; period-1 charge is effective
        assert body["priceAmount"] == "25.00"
        assert body["effectiveAmount"] == "20.00"
        assert body["discountType"] == "percent"
        assert body["discountPeriodsRemaining"] == 2  # 3 total - period 1
        assert body["promoCode"] == "SUMMER"
        assert _last_charge(mock_clients) == Decimal("20.00")

    @pytest.mark.asyncio
    async def test_absolute_discount(self, client, mock_clients):
        r = await client.post(CREATE, json=_create_body(
            discountType="absolute", discountValue="5.00", discountPeriodsTotal=1,
        ))
        assert r.status_code == 201
        assert _last_charge(mock_clients) == Decimal("20.00")  # 25 - 5
        assert r.json()["discountPeriodsRemaining"] == 0  # single → none left

    @pytest.mark.asyncio
    async def test_no_discount_charges_full(self, client, mock_clients):
        r = await client.post(CREATE, json=_create_body())
        assert r.status_code == 201
        assert _last_charge(mock_clients) == Decimal("25.00")
        assert r.json()["discountPeriodsRemaining"] == 0
        assert r.json()["effectiveAmount"] == "25.00"


class TestRenewalCounter:
    @pytest.mark.asyncio
    async def test_multi_period_worked_example(self, client, mock_clients):
        """create $20 (rem 2) → renew $20 (rem 1) → renew $20 (rem 0) → renew $25."""
        sub_id = (await client.post(CREATE, json=_create_body(
            discountType="percent", discountValue="20", discountPeriodsTotal=3,
        ))).json()["id"]

        b1 = await _renew(client, sub_id, mock_clients)
        assert _last_charge(mock_clients) == Decimal("20.00")
        assert b1["discountPeriodsRemaining"] == 1

        b2 = await _renew(client, sub_id, mock_clients)
        assert _last_charge(mock_clients) == Decimal("20.00")
        assert b2["discountPeriodsRemaining"] == 0

        b3 = await _renew(client, sub_id, mock_clients)
        assert _last_charge(mock_clients) == Decimal("25.00")  # promo done
        assert b3["discountPeriodsRemaining"] == 0
        assert b3["effectiveAmount"] == "25.00"

    @pytest.mark.asyncio
    async def test_single_period_renews_at_full(self, client, mock_clients):
        sub_id = (await client.post(CREATE, json=_create_body(
            discountType="percent", discountValue="20", discountPeriodsTotal=1,
        ))).json()["id"]
        await _renew(client, sub_id, mock_clients)
        assert _last_charge(mock_clients) == Decimal("25.00")

    @pytest.mark.asyncio
    async def test_perpetual_never_decrements(self, client, mock_clients):
        sub_id = (await client.post(CREATE, json=_create_body(
            discountType="percent", discountValue="20", discountPeriodsTotal=-1,
        ))).json()["id"]
        assert (await client.get(f"{CREATE}/{sub_id}")).json()["discountPeriodsRemaining"] == -1
        for _ in range(3):
            b = await _renew(client, sub_id, mock_clients)
            assert _last_charge(mock_clients) == Decimal("20.00")
            assert b["discountPeriodsRemaining"] == -1


class TestPlanChangeEndsPromo:
    @pytest.mark.asyncio
    async def test_pending_plan_change_clears_discount(self, client, mock_clients):
        from unittest.mock import AsyncMock

        from sqlalchemy import update

        from bss_models.subscription import Subscription

        sub_id = (await client.post(CREATE, json=_create_body(
            discountType="percent", discountValue="20", discountPeriodsTotal=3,
        ))).json()["id"]
        await client.post(
            f"{CREATE}/{sub_id}/schedule-plan-change", json={"newOfferingId": "PLAN_L"}
        )

        # Backdate the pivot so this renew applies the plan change.
        db_session = client._transport.app.state.session_factory()._session  # type: ignore[attr-defined]
        await db_session.execute(
            update(Subscription)
            .where(Subscription.id == sub_id)
            .values(pending_effective_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
        )
        await db_session.flush()

        mock_clients["catalog"].get_offering = AsyncMock(return_value={
            "id": "PLAN_L", "name": "Max",
            "productOfferingPrice": [{
                "id": "PRICE_PLAN_L", "priceType": "recurring",
                "price": {"taxIncludedAmount": {"value": "45.00", "unit": "SGD"}},
            }],
            "bundleAllowance": [{"allowanceType": "data", "quantity": 153600, "unit": "mb"}],
        })

        b = await _renew(client, sub_id, mock_clients)
        # plan change ends the promo: full new price, discount fields cleared.
        assert _last_charge(mock_clients) == Decimal("45.00")
        assert b["discountType"] is None
        assert b["discountPeriodsRemaining"] == 0
        assert b["promoCode"] is None
        assert b["effectiveAmount"] == "45.00"
