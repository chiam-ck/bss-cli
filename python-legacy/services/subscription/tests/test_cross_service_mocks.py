"""Cross-service error path tests — respx-mocked Payment, Catalog, CRM."""

from unittest.mock import AsyncMock

import pytest
from bss_clients.errors import NotFound, ServerError


@pytest.mark.asyncio
async def test_create_customer_not_found(client, mock_clients):
    mock_clients["crm"].get_customer = AsyncMock(side_effect=NotFound("Not found"))

    resp = await client.post(
        "/subscription-api/v1/subscription",
        json={
            "customerId": "CUST-9999",
            "offeringId": "PLAN_M",
            "msisdn": "90000042",
            "iccid": "8910000000000042",
            "paymentMethodId": "PM-0001",
        },
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "subscription.create.requires_customer"


@pytest.mark.asyncio
async def test_create_msisdn_not_reserved(client, mock_clients):
    mock_clients["inventory"].get_msisdn = AsyncMock(
        return_value={"msisdn": "90000042", "status": "available"}
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
    assert resp.json()["reason"] == "subscription.create.msisdn_and_esim_reserved"


@pytest.mark.asyncio
async def test_create_esim_not_reserved(client, mock_clients):
    mock_clients["inventory"].get_esim = AsyncMock(
        return_value={"iccid": "8910000000000042", "profileState": "available"}
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
    assert resp.json()["reason"] == "subscription.create.msisdn_and_esim_reserved"


@pytest.mark.asyncio
async def test_create_catalog_unavailable(client, mock_clients):
    mock_clients["catalog"].get_offering = AsyncMock(
        side_effect=ServerError(503, "Service unavailable")
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
    assert resp.status_code == 500


@pytest.mark.asyncio
async def test_vas_purchase_catalog_not_found(client, mock_clients):
    # Create subscription first
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

    mock_clients["catalog"].get_vas = AsyncMock(side_effect=NotFound("VAS not found"))

    resp = await client.post(
        f"/subscription-api/v1/subscription/{sub_id}/vas-purchase",
        json={"vasOfferingId": "VAS_NONEXISTENT"},
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "subscription.vas_purchase.vas_offering_sellable"


@pytest.mark.asyncio
async def test_renew_payment_declined(client, mock_clients):
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

    # Override payment to decline for renewal
    mock_clients["payment"].charge = AsyncMock(
        return_value={"id": "PAY-DECLINE", "status": "declined", "declineReason": "insufficient_funds"}
    )

    resp = await client.post(f"/subscription-api/v1/subscription/{sub_id}/renew")
    assert resp.status_code == 200
    assert resp.json()["state"] == "blocked"
