"""TMF635 POST /usage + GET API tests."""

from datetime import datetime, timezone

import pytest

USAGE_PATH = "/tmf-api/usageManagement/v4/usage"


def _payload(**overrides) -> dict:
    body = {
        "msisdn": "90000042",
        "eventType": "data",
        "eventTime": datetime.now(timezone.utc).isoformat(),
        "quantity": 100,
        "unit": "mb",
        "source": "test",
        "rawCdrRef": "CDR-0001",
    }
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_create_usage_happy_path(client):
    resp = await client.post(USAGE_PATH, json=_payload())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"].startswith("UE-")
    assert body["subscriptionId"] == "SUB-0001"
    assert body["msisdn"] == "90000042"
    assert body["eventType"] == "data"
    assert body["quantity"] == 100
    assert body["unit"] == "mb"
    assert body["href"].endswith(body["id"])
    assert body["atType"] == "Usage"


@pytest.mark.asyncio
async def test_create_usage_assigns_sequential_ids(client):
    r1 = await client.post(USAGE_PATH, json=_payload())
    r2 = await client.post(USAGE_PATH, json=_payload(rawCdrRef="CDR-0002"))
    assert r1.status_code == 201
    assert r2.status_code == 201
    id1 = r1.json()["id"]
    id2 = r2.json()["id"]
    assert id1 != id2
    assert int(id1.split("-")[1]) + 1 == int(id2.split("-")[1])


@pytest.mark.asyncio
async def test_create_usage_negative_quantity_rejected(client):
    resp = await client.post(USAGE_PATH, json=_payload(quantity=-5))
    assert resp.status_code == 422
    assert resp.json()["reason"] == "usage.record.positive_quantity"


@pytest.mark.asyncio
async def test_create_usage_zero_quantity_rejected(client):
    resp = await client.post(USAGE_PATH, json=_payload(quantity=0))
    assert resp.status_code == 422
    assert resp.json()["reason"] == "usage.record.positive_quantity"


@pytest.mark.asyncio
async def test_create_usage_invalid_event_type_rejected(client):
    resp = await client.post(USAGE_PATH, json=_payload(eventType="video"))
    assert resp.status_code == 422
    assert resp.json()["reason"] == "usage.record.valid_event_type"


@pytest.mark.asyncio
async def test_get_usage_by_id(client):
    post_resp = await client.post(USAGE_PATH, json=_payload())
    event_id = post_resp.json()["id"]

    get_resp = await client.get(f"{USAGE_PATH}/{event_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == event_id


@pytest.mark.asyncio
async def test_get_usage_by_id_not_found(client):
    resp = await client.get(f"{USAGE_PATH}/UE-999999")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_usage_filter_by_msisdn(client):
    await client.post(USAGE_PATH, json=_payload(quantity=10))
    await client.post(USAGE_PATH, json=_payload(quantity=20))

    resp = await client.get(USAGE_PATH, params={"msisdn": "90000042"})
    assert resp.status_code == 200
    events = resp.json()
    assert len(events) >= 2
    assert all(e["msisdn"] == "90000042" for e in events)


@pytest.mark.asyncio
async def test_list_usage_filter_by_subscription_id(client):
    await client.post(USAGE_PATH, json=_payload(quantity=10))

    resp = await client.get(USAGE_PATH, params={"subscriptionId": "SUB-0001"})
    assert resp.status_code == 200
    events = resp.json()
    assert len(events) >= 1
    assert all(e["subscriptionId"] == "SUB-0001" for e in events)


@pytest.mark.asyncio
async def test_list_usage_filter_by_type(client):
    await client.post(USAGE_PATH, json=_payload(eventType="data", quantity=10))
    await client.post(USAGE_PATH, json=_payload(eventType="voice_minutes", unit="minutes", quantity=3))

    resp = await client.get(USAGE_PATH, params={"type": "voice_minutes"})
    assert resp.status_code == 200
    events = resp.json()
    assert all(e["eventType"] == "voice_minutes" for e in events)


@pytest.mark.asyncio
async def test_list_usage_limit_enforced(client):
    for _ in range(3):
        await client.post(USAGE_PATH, json=_payload())

    resp = await client.get(USAGE_PATH, params={"limit": 2})
    assert resp.status_code == 200
    assert len(resp.json()) <= 2
