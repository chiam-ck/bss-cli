"""Test SOM decomposition logic."""

from unittest.mock import AsyncMock

import pytest
from app.repositories.service_order_repo import ServiceOrderRepository
from app.repositories.service_repo import ServiceRepository
from app.services.som_service import SOMService


async def test_decompose_creates_service_graph(client, db_session):
    """Decomposition should create SO -> CFS -> 2 RFS with reserved inventory."""
    app = client._transport.app
    svc = SOMService(
        session=db_session,
        so_repo=ServiceOrderRepository(db_session),
        svc_repo=ServiceRepository(db_session),
        inventory_client=app.state.inventory_client,
        exchange=None,
    )

    so = await svc.decompose(
        commercial_order_id="ORD-TEST-001",
        customer_id="CUST-0001",
        offering_id="PLAN_M",
        msisdn_preference=None,
        payment_method_id="PM-0001",
    )

    assert so.id.startswith("SO-")
    assert so.state == "in_progress"
    assert so.commercial_order_id == "ORD-TEST-001"

    # Verify inventory was reserved
    inv = app.state.inventory_client
    inv.reserve_next_msisdn.assert_called_once()
    inv.reserve_esim.assert_called_once()


async def test_decompose_carries_price_snapshot_into_cfs_characteristics(
    client, db_session
):
    """v0.7 — priceSnapshot from order.in_progress lands on CFS characteristics."""
    app = client._transport.app
    svc = SOMService(
        session=db_session,
        so_repo=ServiceOrderRepository(db_session),
        svc_repo=ServiceRepository(db_session),
        inventory_client=app.state.inventory_client,
        exchange=None,
    )

    snap = {
        "priceAmount": "25.00",
        "priceCurrency": "SGD",
        "priceOfferingPriceId": "PRICE_PLAN_M",
    }
    so = await svc.decompose(
        commercial_order_id="ORD-TEST-SNAP",
        customer_id="CUST-0001",
        offering_id="PLAN_M",
        msisdn_preference=None,
        payment_method_id="PM-0001",
        price_snapshot=snap,
    )

    # Walk the graph to the CFS and inspect characteristics.
    svc_repo = ServiceRepository(db_session)
    so_repo = ServiceOrderRepository(db_session)
    full_so = await so_repo.get(so.id)
    cfs_id = full_so.items[0].target_service_id
    cfs = await svc_repo.get(cfs_id)
    assert cfs.characteristics["priceSnapshot"] == snap


async def test_decompose_rolls_back_msisdn_on_esim_failure(client, db_session):
    """If eSIM reservation fails, MSISDN must be released."""
    app = client._transport.app
    inv = app.state.inventory_client
    inv.reserve_esim = AsyncMock(side_effect=Exception("eSIM pool exhausted"))

    svc = SOMService(
        session=db_session,
        so_repo=ServiceOrderRepository(db_session),
        svc_repo=ServiceRepository(db_session),
        inventory_client=inv,
        exchange=None,
    )

    with pytest.raises(Exception, match="eSIM pool exhausted"):
        await svc.decompose(
            commercial_order_id="ORD-TEST-002",
            customer_id="CUST-0001",
            offering_id="PLAN_M",
            msisdn_preference=None,
            payment_method_id="PM-0001",
        )

    # MSISDN should have been released in rollback
    inv.release_msisdn.assert_called_once_with("90000042")
