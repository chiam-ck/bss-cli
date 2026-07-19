"""v0.7 — renewal charges the snapshot, not the catalog.

Even when the catalog re-prices an offering between activation and
renewal, the customer is charged what they signed up for.
"""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_renewal_uses_snapshot_amount_not_catalog(client, mock_clients):
    """Customer signed up at $20 (promo); catalog later flips to $30 — renewal charges $20."""
    create = await client.post(
        "/subscription-api/v1/subscription",
        json={
            "customerId": "CUST-0001",
            "offeringId": "PLAN_M",
            "msisdn": "90000042",
            "iccid": "8910000000000042",
            "paymentMethodId": "PM-0001",
            "priceSnapshot": {
                "priceAmount": "20.00",
                "priceCurrency": "SGD",
                "priceOfferingPriceId": "PRICE_PLAN_M",
            },
        },
    )
    assert create.status_code == 201, create.text
    sub_id = create.json()["id"]

    # Catalog gets re-priced to $30 — should not affect this subscription.
    mock_clients["catalog"].get_offering = AsyncMock(return_value={
        "id": "PLAN_M",
        "name": "Standard",
        "productOfferingPrice": [{
            "id": "PRICE_PLAN_M_V2",
            "priceType": "recurring",
            "price": {"taxIncludedAmount": {"value": "30.00", "unit": "SGD"}},
        }],
        "bundleAllowance": [
            {"allowanceType": "data", "quantity": 30720, "unit": "mb"},
            {"allowanceType": "voice", "quantity": -1, "unit": "minutes"},
            {"allowanceType": "sms", "quantity": -1, "unit": "count"},
        ],
    })
    # Reset call recorder so we can inspect the renew-time payment.
    mock_clients["payment"].charge.reset_mock()

    resp = await client.post(f"/subscription-api/v1/subscription/{sub_id}/renew")
    assert resp.status_code == 200, resp.text

    # Verify payment was charged the snapshot amount (20.00), not catalog (30.00).
    charge_call = mock_clients["payment"].charge.await_args
    assert charge_call.kwargs["amount"] == Decimal("20.00")
    assert charge_call.kwargs["currency"] == "SGD"
    assert charge_call.kwargs["purpose"] == "renewal"


@pytest.mark.asyncio
async def test_renewal_does_not_call_catalog_active_price_apis(
    client, mock_clients
):
    """Doctrine: the renewal stack must never query active-price catalog APIs."""
    create = await client.post(
        "/subscription-api/v1/subscription",
        json={
            "customerId": "CUST-0001",
            "offeringId": "PLAN_M",
            "msisdn": "90000042",
            "iccid": "8910000000000042",
            "paymentMethodId": "PM-0001",
        },
    )
    sub_id = create.json()["id"]

    # Reset get_active_price call counter; renew() must never hit it.
    mock_clients["catalog"].get_active_price = AsyncMock()

    resp = await client.post(f"/subscription-api/v1/subscription/{sub_id}/renew")
    assert resp.status_code == 200

    mock_clients["catalog"].get_active_price.assert_not_awaited()
