"""v0.7 — COM stamps the active-price snapshot on the order item."""

import pytest

TMF = "/tmf-api/productOrderingManagement/v4"


@pytest.mark.asyncio
async def test_create_order_stamps_price_snapshot(client, mock_clients):
    resp = await client.post(
        f"{TMF}/productOrder",
        json={"customerId": "CUST-0001", "offeringId": "PLAN_M"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    item = body["items"][0]
    assert item["priceAmount"] == "25.00"
    assert item["priceCurrency"] == "SGD"
    assert item["priceOfferingPriceId"] == "PRICE_PLAN_M"
    # Catalog was queried for the *active* price, not the static get_offering.
    mock_clients["catalog"].get_active_price.assert_awaited_with("PLAN_M")


@pytest.mark.asyncio
async def test_offering_with_no_active_price_rejects_create(client, mock_clients):
    """An offering past its valid window surfaces as policy.offering.not_sellable_now."""
    from unittest.mock import AsyncMock

    from bss_clients.errors import PolicyViolationFromServer

    mock_clients["catalog"].get_active_price = AsyncMock(
        side_effect=PolicyViolationFromServer(
            rule="catalog.price.no_active_row",
            message="No active price",
            context={"offering_id": "PLAN_RETIRED"},
        )
    )
    # The offering still exists for read paths, just not sellable now.
    mock_clients["catalog"].get_offering = AsyncMock(return_value={"id": "PLAN_RETIRED"})

    resp = await client.post(
        f"{TMF}/productOrder",
        json={"customerId": "CUST-0001", "offeringId": "PLAN_RETIRED"},
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["reason"] == "policy.offering.not_sellable_now"


@pytest.mark.asyncio
async def test_submit_order_emits_price_snapshot_in_event(client):
    """The order.in_progress payload carries priceSnapshot for SOM."""
    create = await client.post(
        f"{TMF}/productOrder",
        json={"customerId": "CUST-0001", "offeringId": "PLAN_M"},
    )
    order_id = create.json()["id"]

    # The publish helper writes to audit.domain_event in-DB. Read it back
    # to verify the payload shape.
    from bss_models.audit import DomainEvent
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    db_session: AsyncSession = client._transport.app.state.session_factory()._session  # type: ignore[attr-defined]

    submit = await client.post(f"{TMF}/productOrder/{order_id}/submit")
    assert submit.status_code == 200, submit.text

    rows = (
        await db_session.execute(
            select(DomainEvent)
            .where(DomainEvent.aggregate_id == order_id)
            .where(DomainEvent.event_type == "order.in_progress")
        )
    ).scalars().all()
    assert len(rows) >= 1
    payload = rows[-1].payload
    assert payload["priceSnapshot"] == {
        "priceAmount": "25.00",
        "priceCurrency": "SGD",
        "priceOfferingPriceId": "PRICE_PLAN_M",
    }
