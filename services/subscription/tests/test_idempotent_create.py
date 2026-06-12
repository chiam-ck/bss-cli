"""v1.2 — subscription.create is idempotent on commercial_order_id.

The COM completion handler runs over an MQ message that can be redelivered
(crash before ack, at-least-once delivery). Without idempotency a redelivery
would charge the card-on-file a second time and mint a second subscription.
These tests prove a repeat create for the same commercial order returns the
existing subscription and does NOT charge again — the guard behind Motto #2
(card-on-file) surviving the resilient pipeline.
"""

import uuid

import pytest
from app.repositories.subscription_repo import SubscriptionRepository
from app.repositories.vas_repo import VasPurchaseRepository
from app.services.subscription_service import SubscriptionService


def _svc(db_session, app):
    return SubscriptionService(
        session=db_session,
        repo=SubscriptionRepository(db_session),
        vas_repo=VasPurchaseRepository(db_session),
        crm_client=app.state.crm_client,
        payment_client=app.state.payment_client,
        catalog_client=app.state.catalog_client,
        inventory_client=app.state.inventory_client,
    )


@pytest.mark.asyncio
async def test_repeat_create_same_order_returns_existing_no_double_charge(
    client, db_session, mock_clients
):
    app = client._transport.app
    svc = _svc(db_session, app)

    # Unique identifiers so the shared-DB UNIQUE constraints don't collide.
    run = uuid.uuid4().int % 100000
    msisdn = str(91_500_000 + run)
    iccid = f"891055{uuid.uuid4().int % 10_000_000_000:010d}"
    order_id = f"ORD-IDEM-{run}"

    first = await svc.create(
        customer_id="CUST-0001",
        offering_id="PLAN_M",
        msisdn=msisdn,
        iccid=iccid,
        payment_method_id="PM-0001",
        commercial_order_id=order_id,
    )
    assert mock_clients["payment"].charge.call_count == 1

    # Redelivery: same order, same args.
    second = await svc.create(
        customer_id="CUST-0001",
        offering_id="PLAN_M",
        msisdn=msisdn,
        iccid=iccid,
        payment_method_id="PM-0001",
        commercial_order_id=order_id,
    )

    assert second.id == first.id, "redelivery should return the same subscription"
    assert mock_clients["payment"].charge.call_count == 1, "must NOT charge twice"


@pytest.mark.asyncio
async def test_create_without_order_id_still_charges(client, db_session, mock_clients):
    """Legacy/direct callers (no commercial_order_id) keep the normal path."""
    app = client._transport.app
    svc = _svc(db_session, app)

    run = uuid.uuid4().int % 100000
    sub = await svc.create(
        customer_id="CUST-0001",
        offering_id="PLAN_M",
        msisdn=str(91_600_000 + run),
        iccid=f"891066{uuid.uuid4().int % 10_000_000_000:010d}",
        payment_method_id="PM-0001",
    )
    assert sub.id
    assert mock_clients["payment"].charge.call_count == 1
