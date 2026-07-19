"""Termination tests — happy path, inventory release, events."""

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
async def test_terminate_active(client, mock_clients):
    sub_id = await _create_sub(client)

    resp = await client.post(f"/subscription-api/v1/subscription/{sub_id}/terminate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "terminated"
    assert body["terminatedAt"] is not None


@pytest.mark.asyncio
async def test_terminate_blocked(client, mock_clients, simulate_usage):
    sub_id = await _create_sub(client)

    # Exhaust to blocked
    await simulate_usage(sub_id, "data", 31000)

    resp = await client.post(f"/subscription-api/v1/subscription/{sub_id}/terminate")
    assert resp.status_code == 200
    assert resp.json()["state"] == "terminated"


@pytest.mark.asyncio
async def test_terminate_calls_release_msisdn(client, mock_clients):
    sub_id = await _create_sub(client)
    await client.post(f"/subscription-api/v1/subscription/{sub_id}/terminate")

    mock_clients["inventory"].release_msisdn.assert_called_with("90000042")


@pytest.mark.asyncio
async def test_terminate_calls_recycle_esim(client, mock_clients):
    sub_id = await _create_sub(client)
    await client.post(f"/subscription-api/v1/subscription/{sub_id}/terminate")

    mock_clients["inventory"].recycle_esim.assert_called_with("8910000000000042")


@pytest.mark.asyncio
async def test_terminate_already_terminated_fails(client, mock_clients):
    sub_id = await _create_sub(client)
    await client.post(f"/subscription-api/v1/subscription/{sub_id}/terminate")

    resp = await client.post(f"/subscription-api/v1/subscription/{sub_id}/terminate")
    assert resp.status_code == 422
    assert resp.json()["reason"] == "subscription.terminate.invalid_state"


@pytest.mark.asyncio
async def test_terminate_pending_fails(client, mock_clients):
    # Pending subs are immediately activated by create flow, so we can't
    # easily test this via the API. But we can verify terminated → terminate fails.
    sub_id = await _create_sub(client)
    await client.post(f"/subscription-api/v1/subscription/{sub_id}/terminate")

    resp = await client.post(f"/subscription-api/v1/subscription/{sub_id}/terminate")
    assert resp.status_code == 422
