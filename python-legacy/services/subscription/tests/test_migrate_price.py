"""v0.7 — operator price migration with notice."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest


async def _seed_active_sub(client) -> str:
    resp = await client.post(
        "/subscription-api/v1/subscription",
        json={
            "customerId": "CUST-0001",
            "offeringId": "PLAN_M",
            "msisdn": "90000042",
            "iccid": "8910000000000042",
            "paymentMethodId": "PM-0001",
            "priceSnapshot": {
                "priceAmount": "25.00",
                "priceCurrency": "SGD",
                "priceOfferingPriceId": "PRICE_PLAN_M",
            },
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_migrate_price_writes_pending_for_each_subscription(client, mock_clients):
    """Every matched subscription gets per-row pending fields + per-row events."""
    sub_id = await _seed_active_sub(client)

    # Catalog pretends a new $30 price row was added to PLAN_M.
    mock_clients["catalog"].get_offering_price = AsyncMock(return_value={
        "id": "PRICE_PLAN_M",
        "price": {"taxIncludedAmount": {"value": "30.00", "unit": "SGD"}},
    })
    mock_clients["catalog"].get_offering = AsyncMock(return_value={
        "id": "PLAN_M",
        "productOfferingPrice": [
            {"id": "PRICE_PLAN_M"},
        ],
    })

    effective_from = datetime(2026, 5, 1, tzinfo=timezone.utc)
    resp = await client.post(
        "/subscription-api/v1/admin/subscription/migrate-price",
        json={
            "offeringId": "PLAN_M",
            "newPriceId": "PRICE_PLAN_M",
            "effectiveFrom": effective_from.isoformat(),
            "noticeDays": 30,
            "initiatedBy": "ops-001",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] >= 1
    assert sub_id in body["subscriptionIds"]

    # Verify pending fields land on the row.
    sub = await client.get(f"/subscription-api/v1/subscription/{sub_id}")
    assert sub.status_code == 200
    sub_body = sub.json()
    assert sub_body["pendingOfferingId"] == "PLAN_M"  # same plan = price migration
    assert sub_body["pendingOfferingPriceId"] == "PRICE_PLAN_M"


@pytest.mark.asyncio
async def test_migrate_price_rejects_price_not_on_offering(client, mock_clients):
    await _seed_active_sub(client)

    mock_clients["catalog"].get_offering_price = AsyncMock(return_value={
        "id": "PRICE_PLAN_L",
        "price": {"taxIncludedAmount": {"value": "45.00", "unit": "SGD"}},
    })
    # PLAN_M's offering only lists PRICE_PLAN_M; PRICE_PLAN_L belongs to PLAN_L.
    mock_clients["catalog"].get_offering = AsyncMock(return_value={
        "id": "PLAN_M",
        "productOfferingPrice": [{"id": "PRICE_PLAN_M"}],
    })

    resp = await client.post(
        "/subscription-api/v1/admin/subscription/migrate-price",
        json={
            "offeringId": "PLAN_M",
            "newPriceId": "PRICE_PLAN_L",
            "effectiveFrom": datetime(2026, 5, 1, tzinfo=timezone.utc).isoformat(),
            "noticeDays": 30,
            "initiatedBy": "ops-001",
        },
    )
    assert resp.status_code == 422
    assert (
        resp.json()["reason"]
        == "subscription.migrate_price.price_not_on_offering"
    )


@pytest.mark.asyncio
async def test_migration_renewal_emits_price_migrated_not_plan_changed(
    client, mock_clients
):
    """When the renewal applies a same-plan price migration, the event is
    `subscription.price_migrated` rather than `subscription.plan_changed`."""
    from bss_models.audit import DomainEvent
    from bss_models.subscription import Subscription
    from sqlalchemy import select, update

    sub_id = await _seed_active_sub(client)

    # Backdate pending_effective_at and stage same-offering pending fields.
    db_session = client._transport.app.state.session_factory()._session  # type: ignore[attr-defined]
    await db_session.execute(
        update(Subscription)
        .where(Subscription.id == sub_id)
        .values(
            pending_offering_id="PLAN_M",
            pending_offering_price_id="PRICE_PLAN_M",
            pending_effective_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
    )
    await db_session.flush()

    mock_clients["catalog"].get_offering_price = AsyncMock(return_value={
        "id": "PRICE_PLAN_M",
        "price": {"taxIncludedAmount": {"value": "30.00", "unit": "SGD"}},
    })

    resp = await client.post(f"/subscription-api/v1/subscription/{sub_id}/renew")
    assert resp.status_code == 200, resp.text

    rows = (
        await db_session.execute(
            select(DomainEvent)
            .where(DomainEvent.aggregate_id == sub_id)
            .where(DomainEvent.event_type == "subscription.price_migrated")
        )
    ).scalars().all()
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["newPriceAmount"] == "30.00"
    assert payload["newOfferingId"] == "PLAN_M"

    # And no plan_changed event.
    rows_pc = (
        await db_session.execute(
            select(DomainEvent)
            .where(DomainEvent.aggregate_id == sub_id)
            .where(DomainEvent.event_type == "subscription.plan_changed")
        )
    ).scalars().all()
    assert rows_pc == []
