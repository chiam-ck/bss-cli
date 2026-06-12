"""SOM service — orchestration layer."""


import aio_pika.abc
import structlog
from bss_clients import InventoryClient
from bss_clock import now as clock_now
from bss_models.service_inventory import Service, ServiceOrder
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.events.publisher import publish
from app.policies.service_order import check_service_order_transition, check_service_transition
from app.repositories.service_order_repo import ServiceOrderRepository
from app.repositories.service_repo import ServiceRepository
from app.services.decomposition import decompose_order

log = structlog.get_logger()


class SOMService:
    def __init__(
        self,
        session: AsyncSession,
        so_repo: ServiceOrderRepository,
        svc_repo: ServiceRepository,
        inventory_client: InventoryClient,
        exchange: aio_pika.abc.AbstractExchange | None,
    ):
        self._session = session
        self._so_repo = so_repo
        self._svc_repo = svc_repo
        self._inventory_client = inventory_client
        self._exchange = exchange

    # ── Decompose (order.in_progress) ──────────────────────────────────

    async def decompose(
        self,
        *,
        commercial_order_id: str,
        customer_id: str,
        offering_id: str,
        msisdn_preference: str | None,
        payment_method_id: str,
        price_snapshot: dict | None = None,
    ) -> ServiceOrder:
        """Called when order.in_progress is consumed."""
        return await decompose_order(
            commercial_order_id=commercial_order_id,
            customer_id=customer_id,
            offering_id=offering_id,
            msisdn_preference=msisdn_preference,
            payment_method_id=payment_method_id,
            session=self._session,
            so_repo=self._so_repo,
            svc_repo=self._svc_repo,
            inventory_client=self._inventory_client,
            exchange=self._exchange,
            price_snapshot=price_snapshot,
        )

    # ── Task completed (provisioning.task.completed) ───────────────────

    async def handle_task_completed(
        self,
        *,
        service_id: str,
        task_type: str,
        service_order_id: str,
        commercial_order_id: str,
    ) -> None:
        """Called when provisioning.task.completed is consumed."""
        cfs = await self._svc_repo.get(service_id)
        if not cfs:
            log.error("task.completed.cfs_not_found", service_id=service_id)
            return

        # Update pending tasks
        chars = dict(cfs.characteristics or {})
        pending = dict(chars.get("pendingTasks", {}))
        pending[task_type] = "completed"
        chars["pendingTasks"] = pending
        cfs.characteristics = chars
        flag_modified(cfs, "characteristics")

        log.info(
            "task.completed.updated",
            service_id=service_id,
            task_type=task_type,
            pending_tasks=pending,
        )

        # Check if ALL tasks are completed
        all_completed = all(v == "completed" for v in pending.values())
        if not all_completed:
            await self._svc_repo.update(cfs)
            return

        # All tasks done — activate everything
        now = clock_now()

        # CFS → activated
        check_service_transition(cfs.state, "activated")
        cfs.state = "activated"
        cfs.activated_at = now
        await self._svc_repo.add_state_history(
            cfs.id, "reserved", "activated", reason="all provisioning tasks completed"
        )
        await self._svc_repo.update(cfs)

        # RFS children → activated
        for child in cfs.children:
            check_service_transition(child.state, "activated")
            child.state = "activated"
            child.activated_at = now
            await self._svc_repo.add_state_history(
                child.id, "reserved", "activated", reason="parent CFS activated"
            )
            await self._svc_repo.update(child)

        # ServiceOrder → completed
        so = await self._so_repo.get(service_order_id)
        if so:
            check_service_order_transition(so.state, "completed")
            so.state = "completed"
            so.completed_at = now
            await self._so_repo.update(so)

        # Publish service_order.completed with full payload for COM
        event_payload = {
            "serviceOrderId": service_order_id,
            "commercialOrderId": commercial_order_id or chars.get("commercialOrderId", ""),
            "customerId": chars.get("customerId", ""),
            "offeringId": chars.get("offeringId", ""),
            "msisdn": chars.get("msisdn", ""),
            "iccid": chars.get("iccid", ""),
            "paymentMethodId": chars.get("paymentMethodId", ""),
            "cfsServiceId": cfs.id,
        }
        if chars.get("priceSnapshot"):
            event_payload["priceSnapshot"] = chars["priceSnapshot"]
        await publish(
            self._session,
            event_type="service_order.completed",
            aggregate_type="ServiceOrder",
            aggregate_id=service_order_id,
            payload=event_payload,
            exchange=self._exchange,
        )

        log.info(
            "service_order.completed",
            service_order_id=service_order_id,
            cfs_id=cfs.id,
        )

    # ── Task failed (provisioning.task.failed, permanent) ──────────────

    async def handle_task_failed(
        self,
        *,
        service_id: str,
        task_type: str,
        service_order_id: str,
        commercial_order_id: str,
        permanent: bool = True,
    ) -> None:
        """Called when provisioning.task.failed is consumed."""
        if not permanent:
            log.info("task.failed.transient", service_id=service_id, task_type=task_type)
            return

        cfs = await self._svc_repo.get(service_id)
        if not cfs:
            log.error("task.failed.cfs_not_found", service_id=service_id)
            return

        # Update pending tasks
        chars = dict(cfs.characteristics or {})
        pending = dict(chars.get("pendingTasks", {}))
        pending[task_type] = "failed"
        chars["pendingTasks"] = pending
        cfs.characteristics = chars
        flag_modified(cfs, "characteristics")

        # CFS → failed
        check_service_transition(cfs.state, "failed")
        cfs.state = "failed"
        await self._svc_repo.add_state_history(
            cfs.id, "reserved", "failed", reason=f"provisioning task {task_type} failed permanently"
        )
        await self._svc_repo.update(cfs)

        # Release inventory
        msisdn = chars.get("msisdn")
        iccid = chars.get("iccid")
        if msisdn:
            try:
                await self._inventory_client.release_msisdn(msisdn)
            except Exception:
                log.warning("inventory.release.msisdn.failed", msisdn=msisdn)
        if iccid:
            try:
                await self._inventory_client.release_esim(iccid)
            except Exception:
                log.warning("inventory.release.esim.failed", iccid=iccid[-4:] if len(iccid) > 4 else iccid)

        # ServiceOrder → failed
        so = await self._so_repo.get(service_order_id)
        if so:
            check_service_order_transition(so.state, "failed")
            so.state = "failed"
            so.completed_at = clock_now()
            await self._so_repo.update(so)

        # Publish service_order.failed
        await publish(
            self._session,
            event_type="service_order.failed",
            aggregate_type="ServiceOrder",
            aggregate_id=service_order_id,
            payload={
                "serviceOrderId": service_order_id,
                "commercialOrderId": commercial_order_id or chars.get("commercialOrderId", ""),
                "reason": f"provisioning task {task_type} failed permanently",
            },
            exchange=self._exchange,
        )

        log.info(
            "service_order.failed",
            service_order_id=service_order_id,
            cfs_id=cfs.id,
            task_type=task_type,
        )

    # ── Task stuck (provisioning.task.stuck) ───────────────────────────

    async def handle_task_stuck(
        self,
        *,
        service_id: str,
        task_type: str,
        service_order_id: str,
    ) -> None:
        """Called when provisioning.task.stuck is consumed."""
        cfs = await self._svc_repo.get(service_id)
        if not cfs:
            log.error("task.stuck.cfs_not_found", service_id=service_id)
            return

        # Update pending tasks
        chars = dict(cfs.characteristics or {})
        pending = dict(chars.get("pendingTasks", {}))
        pending[task_type] = "stuck"
        chars["pendingTasks"] = pending
        cfs.characteristics = chars
        flag_modified(cfs, "characteristics")
        await self._svc_repo.update(cfs)

        log.warning(
            "task.stuck.manual_intervention_needed",
            service_id=service_id,
            task_type=task_type,
            service_order_id=service_order_id,
        )

    # ── Read-only operations ───────────────────────────────────────────

    async def get_service_order(self, order_id: str) -> ServiceOrder | None:
        return await self._so_repo.get(order_id)

    async def list_service_orders_for_commercial(
        self, commercial_order_id: str
    ) -> list[ServiceOrder]:
        return await self._so_repo.list_by_commercial_order(commercial_order_id)

    async def get_service(self, service_id: str) -> Service | None:
        return await self._svc_repo.get(service_id)

    async def list_services_for_subscription(
        self, subscription_id: str
    ) -> list[Service]:
        return await self._svc_repo.list_by_subscription(subscription_id)
