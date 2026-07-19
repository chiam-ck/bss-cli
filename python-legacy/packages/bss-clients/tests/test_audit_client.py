"""AuditClient — wraps GET /audit-api/v1/events with camelCase params.

We mock the HTTP layer with ``respx`` and assert both the outgoing
query string and the unwrapping of the response envelope.
"""

from __future__ import annotations

from urllib.parse import parse_qs

import pytest
import respx
from bss_clients import AuditClient
from httpx import Response

BASE_URL = "http://test-service:8000"


@pytest.fixture
def client() -> AuditClient:
    return AuditClient(base_url=BASE_URL)


@pytest.mark.asyncio
@respx.mock
async def test_list_events_unwraps_events_array(client: AuditClient) -> None:
    respx.get(f"{BASE_URL}/audit-api/v1/events").mock(
        return_value=Response(
            200,
            json={
                "events": [
                    {
                        "eventId": "e1",
                        "eventType": "order.created",
                        "aggregateType": "Order",
                        "aggregateId": "ORD-001",
                    }
                ],
                "count": 1,
            },
        )
    )
    events = await client.list_events()
    assert len(events) == 1
    assert events[0]["eventType"] == "order.created"


@pytest.mark.asyncio
@respx.mock
async def test_list_events_sends_filters_as_camelcase_query_params(
    client: AuditClient,
) -> None:
    route = respx.get(f"{BASE_URL}/audit-api/v1/events").mock(
        return_value=Response(200, json={"events": [], "count": 0})
    )
    await client.list_events(
        aggregate_type="Order",
        aggregate_id="ORD-042",
        event_type="order.completed",
        event_type_prefix="order.",
        occurred_since="2026-04-01T00:00:00+00:00",
        occurred_until="2026-04-02T00:00:00+00:00",
        limit=25,
    )
    assert route.call_count == 1
    qs = parse_qs(route.calls[0].request.url.query.decode())
    assert qs["aggregateType"] == ["Order"]
    assert qs["aggregateId"] == ["ORD-042"]
    assert qs["eventType"] == ["order.completed"]
    assert qs["eventTypePrefix"] == ["order."]
    assert qs["occurredSince"] == ["2026-04-01T00:00:00+00:00"]
    assert qs["occurredUntil"] == ["2026-04-02T00:00:00+00:00"]
    assert qs["limit"] == ["25"]


@pytest.mark.asyncio
@respx.mock
async def test_list_events_omits_none_params(client: AuditClient) -> None:
    route = respx.get(f"{BASE_URL}/audit-api/v1/events").mock(
        return_value=Response(200, json={"events": [], "count": 0})
    )
    await client.list_events()
    qs = parse_qs(route.calls[0].request.url.query.decode())
    # Only limit is always sent.
    assert list(qs.keys()) == ["limit"]
    assert qs["limit"] == ["100"]


@pytest.mark.asyncio
@respx.mock
async def test_missing_events_key_returns_empty(client: AuditClient) -> None:
    respx.get(f"{BASE_URL}/audit-api/v1/events").mock(
        return_value=Response(200, json={"count": 0})
    )
    events = await client.list_events()
    assert events == []
