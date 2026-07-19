"""Rating API tests — GET /tariff, POST /rate-test."""

from unittest.mock import AsyncMock

import pytest
from bss_clients.errors import NotFound


@pytest.mark.asyncio
async def test_get_tariff_ok(client):
    resp = await client.get("/rating-api/v1/tariff/PLAN_M")
    assert resp.status_code == 200
    assert resp.json()["id"] == "PLAN_M"


@pytest.mark.asyncio
async def test_get_tariff_not_found(client, mock_clients):
    mock_clients["catalog"].get_offering = AsyncMock(side_effect=NotFound("nope"))
    resp = await client.get("/rating-api/v1/tariff/PLAN_X")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_rate_test_ok(client):
    resp = await client.post(
        "/rating-api/v1/rate-test",
        json={
            "subscriptionId": "SUB-0001",
            "msisdn": "90000042",
            "offeringId": "PLAN_M",
            "eventType": "data",
            "quantity": 100,
            "unit": "mb",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["allowanceType"] == "data"
    assert body["consumedQuantity"] == 100
    assert body["chargeAmount"] == "0"
    assert body["currency"] == "SGD"


@pytest.mark.asyncio
async def test_rate_test_voice(client):
    resp = await client.post(
        "/rating-api/v1/rate-test",
        json={
            "subscriptionId": "SUB-0001",
            "msisdn": "90000042",
            "offeringId": "PLAN_M",
            "eventType": "voice_minutes",
            "quantity": 5,
            "unit": "minutes",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["allowanceType"] == "voice"


@pytest.mark.asyncio
async def test_rate_test_unknown_event_type_422(client):
    resp = await client.post(
        "/rating-api/v1/rate-test",
        json={
            "subscriptionId": "SUB-0001",
            "msisdn": "90000042",
            "offeringId": "PLAN_M",
            "eventType": "video",
            "quantity": 100,
            "unit": "mb",
        },
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "RATING_ERROR"


@pytest.mark.asyncio
async def test_rate_test_offering_not_found_404(client, mock_clients):
    mock_clients["catalog"].get_offering = AsyncMock(side_effect=NotFound("nope"))
    resp = await client.post(
        "/rating-api/v1/rate-test",
        json={
            "subscriptionId": "SUB-0001",
            "msisdn": "90000042",
            "offeringId": "PLAN_X",
            "eventType": "data",
            "quantity": 100,
            "unit": "mb",
        },
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["service"] == "rating"
