"""SOM TMF641/TMF638 API tests."""

from bss_models.service_inventory import Service, ServiceOrder, ServiceOrderItem


async def _seed_service_order(client, so_id="SO-9001"):
    """Insert a service order with items directly via test session."""
    app = client._transport.app
    async with app.state.session_factory() as session:
        so = ServiceOrder(
            id=so_id,
            commercial_order_id="ORD-9001",
            state="in_progress",
        )
        session.add(so)
        soi = ServiceOrderItem(
            id="SOI-9001",
            service_order_id=so_id,
            action="add",
            service_spec_id="MobileBroadband",
            target_service_id="SVC-9001",
        )
        session.add(soi)
        await session.flush()


async def _seed_service(client, svc_id="SVC-9001"):
    """Insert a CFS with children via test session."""
    app = client._transport.app
    async with app.state.session_factory() as session:
        cfs = Service(
            id=svc_id,
            spec_id="MobileBroadband",
            type="CFS",
            state="reserved",
            characteristics={
                "msisdn": "90000042",
                "iccid": "8910000000000042",
                "pendingTasks": {
                    "HLR_PROVISION": "completed",
                    "PCRF_POLICY_PUSH": "completed",
                    "OCS_BALANCE_INIT": "pending",
                    "ESIM_PROFILE_PREPARE": "pending",
                },
            },
        )
        rfs_data = Service(
            id="SVC-9002",
            spec_id="DataService",
            type="RFS",
            parent_service_id=svc_id,
            state="reserved",
        )
        rfs_voice = Service(
            id="SVC-9003",
            spec_id="VoiceService",
            type="RFS",
            parent_service_id=svc_id,
            state="reserved",
        )
        session.add_all([cfs, rfs_data, rfs_voice])
        await session.flush()


async def test_get_service_order(client):
    await _seed_service_order(client, "SO-9001")
    resp = await client.get(
        "/tmf-api/serviceOrderingManagement/v4/serviceOrder/SO-9001"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "SO-9001"
    assert body["commercialOrderId"] == "ORD-9001"
    assert body["state"] == "in_progress"
    assert body["@type"] == "ServiceOrder"
    assert len(body["items"]) == 1
    assert body["items"][0]["action"] == "add"


async def test_get_service_order_not_found(client):
    resp = await client.get(
        "/tmf-api/serviceOrderingManagement/v4/serviceOrder/SO-9999"
    )
    assert resp.status_code == 404


async def test_list_service_orders_by_commercial_id(client):
    await _seed_service_order(client, "SO-9002")
    resp = await client.get(
        "/tmf-api/serviceOrderingManagement/v4/serviceOrder",
        params={"commercialOrderId": "ORD-9001"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) >= 1


async def test_get_service(client):
    await _seed_service(client, "SVC-9001")
    resp = await client.get(
        "/tmf-api/serviceInventoryManagement/v4/service/SVC-9001"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "SVC-9001"
    assert body["type"] == "CFS"
    assert body["state"] == "reserved"
    assert body["@type"] == "Service"
    assert body["characteristics"]["msisdn"] == "90000042"
    assert len(body["children"]) == 2


async def test_get_service_not_found(client):
    resp = await client.get(
        "/tmf-api/serviceInventoryManagement/v4/service/SVC-9999"
    )
    assert resp.status_code == 404
