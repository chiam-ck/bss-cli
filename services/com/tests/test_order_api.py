"""COM order API tests — full lifecycle."""

from unittest.mock import AsyncMock

from bss_clients.errors import NotFound

TMF = "/tmf-api/productOrderingManagement/v4"


async def _create_order(client):
    """Helper — create an acknowledged order."""
    return await client.post(
        f"{TMF}/productOrder",
        json={
            "customerId": "CUST-0001",
            "offeringId": "PLAN_M",
            "msisdnPreference": "9000",
            "notes": "Test order",
        },
    )


# ── Create Order ──────────────────────────────────────────────────────

async def test_create_order(client):
    resp = await _create_order(client)
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"].startswith("ORD-")
    assert body["state"] == "acknowledged"
    assert body["customerId"] == "CUST-0001"
    assert body["msisdnPreference"] == "9000"
    assert body["notes"] == "Test order"
    assert body["@type"] == "ProductOrder"
    assert len(body["items"]) == 1
    assert body["items"][0]["action"] == "add"
    assert body["items"][0]["offeringId"] == "PLAN_M"


async def test_create_order_customer_not_found(client, mock_clients):
    mock_clients["crm"].get_customer = AsyncMock(side_effect=NotFound("not found"))
    resp = await client.post(
        f"{TMF}/productOrder",
        json={
            "customerId": "CUST-9999",
            "offeringId": "PLAN_M",
        },
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["reason"] == "order.create.customer_not_found"


async def test_create_order_offering_not_found(client, mock_clients):
    mock_clients["catalog"].get_offering = AsyncMock(side_effect=NotFound("not found"))
    resp = await client.post(
        f"{TMF}/productOrder",
        json={
            "customerId": "CUST-0001",
            "offeringId": "PLAN_NONEXIST",
        },
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["reason"] == "order.create.offering_not_found"


async def test_create_order_no_payment_method(client, mock_clients):
    mock_clients["payment"].list_methods = AsyncMock(return_value=[])
    resp = await client.post(
        f"{TMF}/productOrder",
        json={
            "customerId": "CUST-0001",
            "offeringId": "PLAN_M",
        },
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["reason"] == "order.create.no_payment_method"


# ── Submit Order ──────────────────────────────────────────────────────

async def test_submit_order(client):
    create_resp = await _create_order(client)
    order_id = create_resp.json()["id"]

    resp = await client.post(f"{TMF}/productOrder/{order_id}/submit")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "in_progress"


async def test_submit_order_not_found(client):
    resp = await client.post(f"{TMF}/productOrder/ORD-9999/submit")
    assert resp.status_code == 422
    body = resp.json()
    assert body["reason"] == "order.not_found"


# ── Cancel Order ──────────────────────────────────────────────────────

async def test_cancel_acknowledged_order(client):
    create_resp = await _create_order(client)
    order_id = create_resp.json()["id"]

    resp = await client.post(f"{TMF}/productOrder/{order_id}/cancel")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "cancelled"


async def test_cancel_in_progress_order_allowed_when_no_som(client, mock_clients):
    """Cancel in_progress allowed if SOM returns no service orders."""
    create_resp = await _create_order(client)
    order_id = create_resp.json()["id"]

    # Submit it first
    await client.post(f"{TMF}/productOrder/{order_id}/submit")

    # SOM returns empty (no service orders started)
    mock_clients["som"].list_for_order = AsyncMock(return_value=[])

    resp = await client.post(f"{TMF}/productOrder/{order_id}/cancel")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "cancelled"


async def test_cancel_in_progress_blocked_by_som(client, mock_clients):
    """Cancel in_progress blocked if SOM has started provisioning."""
    create_resp = await _create_order(client)
    order_id = create_resp.json()["id"]

    # Submit it first
    await client.post(f"{TMF}/productOrder/{order_id}/submit")

    # SOM returns a service order in_progress
    mock_clients["som"].list_for_order = AsyncMock(return_value=[
        {"id": "SO-0001", "state": "in_progress"},
    ])

    resp = await client.post(f"{TMF}/productOrder/{order_id}/cancel")
    assert resp.status_code == 422
    body = resp.json()
    assert body["reason"] == "order.cancel.forbidden_after_som_started"


# ── Get / List ────────────────────────────────────────────────────────

async def test_get_order(client):
    create_resp = await _create_order(client)
    order_id = create_resp.json()["id"]

    resp = await client.get(f"{TMF}/productOrder/{order_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == order_id


async def test_get_order_not_found(client):
    resp = await client.get(f"{TMF}/productOrder/ORD-9999")
    assert resp.status_code == 404


async def test_list_orders_by_customer(client):
    await _create_order(client)
    resp = await client.get(
        f"{TMF}/productOrder",
        params={"customerId": "CUST-0001"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) >= 1
    assert all(o["customerId"] == "CUST-0001" for o in body)


# ── v1.6 — cross-customer queue (cockpit CRM) ────────────────────────

async def test_list_orders_without_customer_returns_all(client):
    await _create_order(client)
    resp = await client.get(f"{TMF}/productOrder")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) >= 1


async def test_list_orders_state_filter_and_paging(client):
    # Runs against the shared dev DB — assert relative properties, not
    # absolute counts (other suites/scenarios leave orders behind).
    await _create_order(client)
    resp = await client.get(
        f"{TMF}/productOrder",
        params={"state": "acknowledged", "limit": 1, "offset": 0},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["state"] == "acknowledged"

    resp = await client.get(
        f"{TMF}/productOrder", params={"state": "no_such_state"}
    )
    assert resp.status_code == 200
    assert resp.json() == []


# ── State Machine Violations ─────────────────────────────────────────

async def test_cannot_submit_cancelled_order(client):
    create_resp = await _create_order(client)
    order_id = create_resp.json()["id"]

    # Cancel it first
    await client.post(f"{TMF}/productOrder/{order_id}/cancel")

    # Try to submit cancelled order
    resp = await client.post(f"{TMF}/productOrder/{order_id}/submit")
    assert resp.status_code == 422
    body = resp.json()
    assert body["reason"] == "order.transition.invalid"
