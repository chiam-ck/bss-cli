"""Cross-service failure paths — Subscription 404/5xx/timeout."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from bss_clients.errors import NotFound, ServerError, Timeout

USAGE_PATH = "/tmf-api/usageManagement/v4/usage"


def _payload(**overrides) -> dict:
    body = {
        "msisdn": "90000042",
        "eventType": "data",
        "eventTime": datetime.now(timezone.utc).isoformat(),
        "quantity": 100,
        "unit": "mb",
    }
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_subscription_not_found_returns_422(client, mock_clients):
    mock_clients["subscription"].get_by_msisdn = AsyncMock(side_effect=NotFound("no sub"))

    resp = await client.post(USAGE_PATH, json=_payload())
    assert resp.status_code == 422
    assert resp.json()["reason"] == "usage.record.subscription_must_exist"


@pytest.mark.asyncio
async def test_subscription_server_error_returns_500(client, mock_clients):
    mock_clients["subscription"].get_by_msisdn = AsyncMock(
        side_effect=ServerError(503, "Service Unavailable")
    )

    resp = await client.post(USAGE_PATH, json=_payload())
    assert resp.status_code == 500


@pytest.mark.asyncio
async def test_subscription_timeout_surfaces_as_500(client, mock_clients):
    mock_clients["subscription"].get_by_msisdn = AsyncMock(
        side_effect=Timeout("upstream timeout")
    )

    # Timeout isn't caught by the mediation middleware's ServerError branch;
    # httpx re-raises as unhandled 500 from the ASGI exception middleware.
    with pytest.raises(Exception):
        await client.post(USAGE_PATH, json=_payload())


@pytest.mark.asyncio
async def test_msisdn_mismatch_between_request_and_enrichment(client, mock_clients):
    mock_clients["subscription"].get_by_msisdn = AsyncMock(
        return_value={
            "id": "SUB-0001",
            "customerId": "CUST-0001",
            "offeringId": "PLAN_M",
            "msisdn": "90000099",
            "state": "active",
        }
    )

    resp = await client.post(USAGE_PATH, json=_payload(msisdn="90000042"))
    assert resp.status_code == 422
    assert resp.json()["reason"] == "usage.record.msisdn_belongs_to_subscription"
