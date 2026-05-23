"""RabbitMQ consumer for SOM — order.in_progress + provisioning task events.

v1.2 — built on ``bss_events.bind_consumer``: every queue (order.in_progress,
provisioning.task.completed/failed/stuck) has a retry + dead-letter (parked)
path and is deduped via the ``service_inventory.processed_event`` inbox keyed on
the relay's ``message_id`` (= the durable ``event_id``). A handler exception
retries with backoff and finally parks instead of dropping the work. The helper
owns the session commit; handlers must not commit.
"""

import aio_pika
import structlog
from bss_events import bind_consumer, declare_retry_exchange
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.service_order_repo import ServiceOrderRepository
from app.repositories.service_repo import ServiceRepository
from app.services.som_service import SOMService

log = structlog.get_logger()

INBOX_SCHEMA = "service_inventory"


def _build_service(app, session: AsyncSession) -> SOMService:
    return SOMService(
        session=session,
        so_repo=ServiceOrderRepository(session),
        svc_repo=ServiceRepository(session),
        inventory_client=app.state.inventory_client,
        exchange=None,  # publisher stages events; no inline publish
    )


async def setup_consumer(app) -> None:
    """Set up MQ consumers in lifespan."""
    settings = app.state.settings
    if not settings.mq_url:
        log.warning("mq.not_configured")
        return

    connection = await aio_pika.connect_robust(settings.mq_url)
    channel = await connection.channel()
    await channel.set_qos(prefetch_count=5)
    exchange = await channel.declare_exchange(
        "bss.events", aio_pika.ExchangeType.TOPIC, durable=True,
    )
    retry_exchange = await declare_retry_exchange(channel)

    app.state.mq_connection = connection
    app.state.mq_exchange = exchange

    max_retries = getattr(settings, "mq_max_retries", 5)
    retry_backoff_ms = getattr(settings, "mq_retry_backoff_ms", 5000)

    async def on_order_in_progress(session: AsyncSession, body: dict) -> None:
        svc = _build_service(app, session)
        await svc.decompose(
            commercial_order_id=body["commercialOrderId"],
            customer_id=body["customerId"],
            offering_id=body["offeringId"],
            msisdn_preference=body.get("msisdnPreference"),
            payment_method_id=body["paymentMethodId"],
            price_snapshot=body.get("priceSnapshot"),
        )

    async def on_task_completed(session: AsyncSession, body: dict) -> None:
        svc = _build_service(app, session)
        await svc.handle_task_completed(
            service_id=body["serviceId"],
            task_type=body["taskType"],
            service_order_id=body["serviceOrderId"],
            commercial_order_id=body.get("commercialOrderId", ""),
        )

    async def on_task_failed(session: AsyncSession, body: dict) -> None:
        svc = _build_service(app, session)
        await svc.handle_task_failed(
            service_id=body["serviceId"],
            task_type=body["taskType"],
            service_order_id=body["serviceOrderId"],
            commercial_order_id=body.get("commercialOrderId", ""),
            permanent=body.get("permanent", True),
        )

    async def on_task_stuck(session: AsyncSession, body: dict) -> None:
        svc = _build_service(app, session)
        await svc.handle_task_stuck(
            service_id=body["serviceId"],
            task_type=body["taskType"],
            service_order_id=body["serviceOrderId"],
        )

    queues = [
        ("som.order.in_progress", "order.in_progress", on_order_in_progress),
        ("som.provisioning.task.completed", "provisioning.task.completed", on_task_completed),
        ("som.provisioning.task.failed", "provisioning.task.failed", on_task_failed),
        ("som.provisioning.task.stuck", "provisioning.task.stuck", on_task_stuck),
    ]
    for queue_name, routing_key, handler in queues:
        await bind_consumer(
            channel=channel,
            exchange=exchange,
            retry_exchange=retry_exchange,
            queue_name=queue_name,
            routing_key=routing_key,
            handler=handler,
            session_factory=app.state.session_factory,
            inbox_schema=INBOX_SCHEMA,
            max_retries=max_retries,
            retry_backoff_ms=retry_backoff_ms,
        )
