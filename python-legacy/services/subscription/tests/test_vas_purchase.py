"""VAS purchase tests — happy path, blocked→active, declined, terminated."""

from unittest.mock import AsyncMock

import pytest


async def _create_sub(client):
    resp = await client.post(
        "/subscription-api/v1/subscription",
        json={
            "customerId": "CUST-0001",
            "offeringId": "PLAN_M",
            "msisdn": "90000042",
            "iccid": "8910000000000042",
            "paymentMethodId": "PM-0001",
        },
    )
    assert resp.status_code == 201
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_vas_purchase_active(client, mock_clients):
    sub_id = await _create_sub(client)

    resp = await client.post(
        f"/subscription-api/v1/subscription/{sub_id}/vas-purchase",
        json={"vasOfferingId": "VAS_DATA_1GB"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "active"

    # Check balance increased
    bal_resp = await client.get(f"/subscription-api/v1/subscription/{sub_id}/balance")
    data_bal = next(b for b in bal_resp.json() if b["allowanceType"] == "data")
    assert data_bal["total"] == 30720 + 1024  # original + VAS


@pytest.mark.asyncio
async def test_vas_purchase_blocked_to_active(client, mock_clients, simulate_usage):
    sub_id = await _create_sub(client)

    # Exhaust data
    await simulate_usage(sub_id, "data", 31000)

    # Verify blocked
    get_resp = await client.get(f"/subscription-api/v1/subscription/{sub_id}")
    assert get_resp.json()["state"] == "blocked"

    # VAS purchase should unblock
    resp = await client.post(
        f"/subscription-api/v1/subscription/{sub_id}/vas-purchase",
        json={"vasOfferingId": "VAS_DATA_1GB"},
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "active"


@pytest.mark.asyncio
async def test_vas_purchase_declined(client, mock_clients):
    sub_id = await _create_sub(client)

    # Override payment to decline
    mock_clients["payment"].charge = AsyncMock(
        return_value={"id": "PAY-DECLINE", "status": "declined", "declineReason": "card_declined"}
    )

    resp = await client.post(
        f"/subscription-api/v1/subscription/{sub_id}/vas-purchase",
        json={"vasOfferingId": "VAS_DATA_1GB"},
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "subscription.vas_purchase.requires_active_cof"

    # State unchanged
    get_resp = await client.get(f"/subscription-api/v1/subscription/{sub_id}")
    assert get_resp.json()["state"] == "active"


@pytest.mark.asyncio
async def test_vas_purchase_terminated(client, mock_clients):
    sub_id = await _create_sub(client)
    await client.post(f"/subscription-api/v1/subscription/{sub_id}/terminate")

    resp = await client.post(
        f"/subscription-api/v1/subscription/{sub_id}/vas-purchase",
        json={"vasOfferingId": "VAS_DATA_1GB"},
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "subscription.vas_purchase.not_if_terminated"
