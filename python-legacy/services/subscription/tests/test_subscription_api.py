"""API tests for subscription endpoints — httpx.AsyncClient with camelCase JSON."""

import pytest


@pytest.mark.asyncio
async def test_create_subscription(client, mock_clients):
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
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["customerId"] == "CUST-0001"
    assert body["offeringId"] == "PLAN_M"
    assert body["msisdn"] == "90000042"
    assert body["iccid"] == "8910000000000042"
    assert body["state"] == "active"
    assert body["id"].startswith("SUB-")
    assert "balances" in body
    assert len(body["balances"]) == 3


@pytest.mark.asyncio
async def test_create_subscription_payment_declined(client, mock_clients):
    mock_clients["payment"].charge = pytest.importorskip("unittest.mock").AsyncMock(
        return_value={"id": "PAY-000002", "status": "declined", "declineReason": "insufficient_funds"}
    )
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
    assert resp.status_code == 422
    body = resp.json()
    assert body["reason"] == "subscription.create.requires_payment_success"


@pytest.mark.asyncio
async def test_get_subscription(client):
    # Create first
    create_resp = await client.post(
        "/subscription-api/v1/subscription",
        json={
            "customerId": "CUST-0001",
            "offeringId": "PLAN_M",
            "msisdn": "90000042",
            "iccid": "8910000000000042",
            "paymentMethodId": "PM-0001",
        },
    )
    sub_id = create_resp.json()["id"]

    resp = await client.get(f"/subscription-api/v1/subscription/{sub_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == sub_id
    assert resp.json()["state"] == "active"


@pytest.mark.asyncio
async def test_get_subscription_not_found(client):
    resp = await client.get("/subscription-api/v1/subscription/SUB-9999")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_subscriptions_for_customer(client):
    await client.post(
        "/subscription-api/v1/subscription",
        json={
            "customerId": "CUST-0001",
            "offeringId": "PLAN_M",
            "msisdn": "90000042",
            "iccid": "8910000000000042",
            "paymentMethodId": "PM-0001",
        },
    )
    resp = await client.get(
        "/subscription-api/v1/subscription",
        params={"customerId": "CUST-0001"},
    )
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


@pytest.mark.asyncio
async def test_list_subscriptions_requires_customer_id(client):
    resp = await client.get("/subscription-api/v1/subscription")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_get_by_msisdn(client):
    await client.post(
        "/subscription-api/v1/subscription",
        json={
            "customerId": "CUST-0001",
            "offeringId": "PLAN_M",
            "msisdn": "90000042",
            "iccid": "8910000000000042",
            "paymentMethodId": "PM-0001",
        },
    )
    resp = await client.get("/subscription-api/v1/subscription/by-msisdn/90000042")
    assert resp.status_code == 200
    assert resp.json()["msisdn"] == "90000042"


@pytest.mark.asyncio
async def test_get_by_msisdn_not_found(client):
    resp = await client.get("/subscription-api/v1/subscription/by-msisdn/99999999")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_balance(client):
    create_resp = await client.post(
        "/subscription-api/v1/subscription",
        json={
            "customerId": "CUST-0001",
            "offeringId": "PLAN_M",
            "msisdn": "90000042",
            "iccid": "8910000000000042",
            "paymentMethodId": "PM-0001",
        },
    )
    sub_id = create_resp.json()["id"]

    resp = await client.get(f"/subscription-api/v1/subscription/{sub_id}/balance")
    assert resp.status_code == 200
    balances = resp.json()
    assert len(balances) == 3
    data_bal = next(b for b in balances if b["allowanceType"] == "data")
    assert data_bal["total"] == 30720
    assert data_bal["consumed"] == 0


@pytest.mark.asyncio
async def test_handle_usage_rated_decrements_balance(client, simulate_usage):
    create_resp = await client.post(
        "/subscription-api/v1/subscription",
        json={
            "customerId": "CUST-0001",
            "offeringId": "PLAN_M",
            "msisdn": "90000042",
            "iccid": "8910000000000042",
            "paymentMethodId": "PM-0001",
        },
    )
    sub_id = create_resp.json()["id"]

    await simulate_usage(sub_id, "data", 1000)

    bal_resp = await client.get(f"/subscription-api/v1/subscription/{sub_id}/balance")
    data_bal = next(b for b in bal_resp.json() if b["allowanceType"] == "data")
    assert data_bal["consumed"] == 1000


@pytest.mark.asyncio
async def test_handle_usage_rated_exhausts_and_blocks(client, simulate_usage):
    create_resp = await client.post(
        "/subscription-api/v1/subscription",
        json={
            "customerId": "CUST-0001",
            "offeringId": "PLAN_M",
            "msisdn": "90000042",
            "iccid": "8910000000000042",
            "paymentMethodId": "PM-0001",
        },
    )
    sub_id = create_resp.json()["id"]

    # Exhaust data (30720 MB)
    await simulate_usage(sub_id, "data", 31000)

    get_resp = await client.get(f"/subscription-api/v1/subscription/{sub_id}")
    assert get_resp.json()["state"] == "blocked"


@pytest.mark.asyncio
async def test_renew_subscription(client):
    create_resp = await client.post(
        "/subscription-api/v1/subscription",
        json={
            "customerId": "CUST-0001",
            "offeringId": "PLAN_M",
            "msisdn": "90000042",
            "iccid": "8910000000000042",
            "paymentMethodId": "PM-0001",
        },
    )
    sub_id = create_resp.json()["id"]

    resp = await client.post(f"/subscription-api/v1/subscription/{sub_id}/renew")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "active"


@pytest.mark.asyncio
async def test_renew_terminated_fails(client):
    create_resp = await client.post(
        "/subscription-api/v1/subscription",
        json={
            "customerId": "CUST-0001",
            "offeringId": "PLAN_M",
            "msisdn": "90000042",
            "iccid": "8910000000000042",
            "paymentMethodId": "PM-0001",
        },
    )
    sub_id = create_resp.json()["id"]

    await client.post(f"/subscription-api/v1/subscription/{sub_id}/terminate")

    resp = await client.post(f"/subscription-api/v1/subscription/{sub_id}/renew")
    assert resp.status_code == 422
    assert resp.json()["reason"] == "subscription.renew.only_if_active_or_blocked"
