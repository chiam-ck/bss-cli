"""Pure decomposition logic — breaks a commercial order into service graph + inventory."""

import aio_pika.abc
import structlog
from bss_clients import InventoryClient
from bss_clock import now as clock_now
from bss_models.service_inventory import Service, ServiceOrder, ServiceOrderItem
from bss_telemetry import semconv, tracer
from sqlalchemy.ext.asyncio import AsyncSession

from app.events.publisher import publish
from app.policies.service_order import check_service_order_transition, check_service_transition
from app.repositories.service_order_repo import ServiceOrderRepository
from app.repositories.service_repo import ServiceRepository

log = structlog.get_logger()

_TASK_TYPES = (
    "HLR_PROVISION",
    "PCRF_POLICY_PUSH",
    "OCS_BALANCE_INIT",
    "ESIM_PROFILE_PREPARE",
)


async def decompose_order(
    *,
    commercial_order_id: str,
    customer_id: str,
    offering_id: str,
    msisdn_preference: str | None,
    payment_method_id: str,
    session: AsyncSession,
    so_repo: ServiceOrderRepository,
    svc_repo: ServiceRepository,
    inventory_client: InventoryClient,
    exchange: aio_pika.abc.AbstractExchange | None,
    price_snapshot: dict | None = None,
) -> ServiceOrder:
    """Decompose a commercial order into ServiceOrder -> CFS -> RFS + inventory."""

    with tracer("bss-som").start_as_current_span("som.decompose") as span:
        span.set_attribute(semconv.BSS_ORDER_ID, commercial_order_id)
        span.set_attribute(semconv.BSS_CUSTOMER_ID, customer_id)
        span.set_attribute(semconv.BSS_OFFERING_ID, offering_id)

        # ── 1. Create ServiceOrder (acknowledged) ──────────────────────────
        so_id = await so_repo.next_order_id()
        so = ServiceOrder(
            id=so_id,
            commercial_order_id=commercial_order_id,
            state="acknowledged",
        )
        await so_repo.create(so)
        span.set_attribute(semconv.BSS_SERVICE_ORDER_ID, so.id)

        # ── 2. Create ServiceOrderItem ─────────────────────────────────────
        soi_id = await so_repo.next_item_id()
        soi = ServiceOrderItem(
            id=soi_id,
            service_order_id=so.id,
            action="add",
            service_spec_id="MobileBroadband",
        )
        session.add(soi)
        await session.flush()

        # ── 3. Create CFS (designed) ───────────────────────────────────────
        cfs_id = await svc_repo.next_id()
        cfs = Service(
            id=cfs_id,
            spec_id="MobileBroadband",
            type="CFS",
            state="designed",
            characteristics={},
        )
        await svc_repo.create(cfs)
        await svc_repo.add_state_history(cfs.id, None, "designed", reason="decomposition")

        # ── 4. Create RFS Data (designed) ──────────────────────────────────
        rfs_data_id = await svc_repo.next_id()
        rfs_data = Service(
            id=rfs_data_id,
            spec_id="DataService",
            type="RFS",
            parent_service_id=cfs.id,
            state="designed",
            characteristics={},
        )
        await svc_repo.create(rfs_data)
        await svc_repo.add_state_history(rfs_data.id, None, "designed", reason="decomposition")

        # ── 5. Create RFS Voice (designed) ─────────────────────────────────
        rfs_voice_id = await svc_repo.next_id()
        rfs_voice = Service(
            id=rfs_voice_id,
            spec_id="VoiceService",
            type="RFS",
            parent_service_id=cfs.id,
            state="designed",
            characteristics={},
        )
        await svc_repo.create(rfs_voice)
        await svc_repo.add_state_history(rfs_voice.id, None, "designed", reason="decomposition")

        # Link SOI to CFS
        soi.target_service_id = cfs.id

        # RFS → reserved (they're part of the reserved inventory graph)
        check_service_transition(rfs_data.state, "reserved")
        rfs_data.state = "reserved"
        await svc_repo.add_state_history(rfs_data.id, "designed", "reserved", reason="inventory reserved")

        check_service_transition(rfs_voice.state, "reserved")
        rfs_voice.state = "reserved"
        await svc_repo.add_state_history(rfs_voice.id, "designed", "reserved", reason="inventory reserved")

        await session.flush()

        # ── 6-7. Reserve inventory ─────────────────────────────────────────
        msisdn_result = None
        esim_result = None
        try:
            msisdn_result = await inventory_client.reserve_next_msisdn(preference=msisdn_preference)
            esim_result = await inventory_client.reserve_esim()
        except Exception:
            # Rollback whatever was reserved
            if msisdn_result:
                try:
                    await inventory_client.release_msisdn(msisdn_result["msisdn"])
                except Exception:
                    log.warning("inventory.rollback.msisdn.failed", msisdn=msisdn_result.get("msisdn"))
            if esim_result:
                try:
                    await inventory_client.release_esim(esim_result["iccid"])
                except Exception:
                    log.warning("inventory.rollback.esim.failed", iccid=esim_result.get("iccid"))
            raise

        msisdn = msisdn_result["msisdn"]
        iccid = esim_result["iccid"]
        imsi = esim_result.get("imsi", "")
        activation_code = esim_result.get("activationCode", esim_result.get("activation_code", ""))
        if len(msisdn) >= 4:
            span.set_attribute(semconv.BSS_MSISDN_LAST4, msisdn[-4:])
        if len(iccid) >= 4:
            span.set_attribute(semconv.BSS_ICCID_LAST4, iccid[-4:])

        # ── 8. Store resources + pending tasks in CFS characteristics ──────
        pending_tasks = {t: "pending" for t in _TASK_TYPES}
        characteristics = {
            "msisdn": msisdn,
            "iccid": iccid,
            "imsi": imsi,
            "activationCode": activation_code,
            "pendingTasks": pending_tasks,
            "commercialOrderId": commercial_order_id,
            "customerId": customer_id,
            "offeringId": offering_id,
            "paymentMethodId": payment_method_id,
        }
        if price_snapshot is not None:
            characteristics["priceSnapshot"] = price_snapshot
        cfs.characteristics = characteristics

        # ── 9. CFS: designed → reserved ────────────────────────────────────
        check_service_transition(cfs.state, "reserved")
        cfs.state = "reserved"
        await svc_repo.add_state_history(cfs.id, "designed", "reserved", reason="inventory reserved")
        await session.flush()

        # ── 10. SO: acknowledged → in_progress ─────────────────────────────
        check_service_order_transition(so.state, "in_progress")
        so.state = "in_progress"
        so.started_at = clock_now()
        await so_repo.update(so)

        await publish(
            session,
            event_type="service_order.in_progress",
            aggregate_type="ServiceOrder",
            aggregate_id=so.id,
            payload={"serviceOrderId": so.id, "commercialOrderId": commercial_order_id},
            exchange=exchange,
        )

        # ── 11. Publish 4 provisioning.task.created events ─────────────────
        for task_type in _TASK_TYPES:
            task_payload = {
                "serviceId": cfs.id,
                "serviceOrderId": so.id,
                "commercialOrderId": commercial_order_id,
                "taskType": task_type,
                "payload": {
                    "msisdn": msisdn,
                    "iccid": iccid,
                    "imsi": imsi,
                    "activationCode": activation_code,
                    "customerId": customer_id,
                    "offeringId": offering_id,
                },
            }
            await publish(
                session,
                event_type="provisioning.task.created",
                aggregate_type="ProvisioningTask",
                aggregate_id=f"{cfs.id}:{task_type}",
                payload=task_payload,
                exchange=exchange,
            )

        log.info(
            "decomposition.completed",
            service_order_id=so.id,
            cfs_id=cfs.id,
            msisdn=msisdn,
            iccid=iccid[-4:] if len(iccid) > 4 else iccid,
        )

        return so
