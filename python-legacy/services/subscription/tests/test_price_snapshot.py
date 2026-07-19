"""v0.7 — subscription persists snapshot from create payload."""

import pytest


@pytest.mark.asyncio
async def test_create_persists_explicit_snapshot(client):
    """priceSnapshot in the request body is what lands on the row."""
    resp = await client.post(
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
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # Even though catalog says $25, we honour the snapshot from the caller.
    assert body["priceAmount"] == "20.00"
    assert body["priceCurrency"] == "SGD"
    assert body["priceOfferingPriceId"] == "PRICE_PLAN_M"


@pytest.mark.asyncio
async def test_create_without_snapshot_falls_back_to_catalog(client):
    """Legacy direct create — snapshot derived from catalog response."""
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
    assert body["priceAmount"] == "25.00"
    assert body["priceOfferingPriceId"] == "PRICE_PLAN_M"
