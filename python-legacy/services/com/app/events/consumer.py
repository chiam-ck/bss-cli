"""RabbitMQ consumer for COM — service_order.completed + service_order.failed.

v1.2 — built on ``bss_events.bind_consumer``: every queue has a retry +
dead-letter (parked) path, and each message is deduped via the
``order_mgmt.processed_event`` inbox keyed on the relay's ``message_id``
(= the durable ``event_id``). A handler exception now retries with backoff and
finally parks — it can no longer silently drop a paid order (the v1.1.3 class).
The helper owns the session commit; handlers must not commit.
"""

import aio_pika
import structlog
from bss_events import bind_consumer, declare_retry_exchange
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.order_repo import OrderRepository
from app.services.order_service import OrderService

log = structlog.get_logger()

INBOX_SCHEMA = "order_mgmt"


def _build_service(app, session: AsyncSession) -> OrderService:
    """Order service for the consume path. Only the deps the handlers use are
    wired; the publisher stages events (no exchange needed)."""
    return OrderService(
        session=session,
        repo=OrderRepository(session),
        crm_client=None,
        catalog_client=None,
        payment_client=None,
        som_client=None,
        subscription_client=app.state.subscription_client,
        loyalty_client=app.state.loyalty_client,
        exchange=None,
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

    max_retries = settings.mq_max_retries
    retry_backoff_ms = settings.mq_retry_backoff_ms

    async def on_service_order_completed(session: AsyncSession, body: dict) -> None:
        svc = _build_service(app, session)
        await svc.handle_service_order_completed(
            commercial_order_id=body["commercialOrderId"],
            customer_id=body["customerId"],
            offering_id=body["offeringId"],
            msisdn=body["msisdn"],
            iccid=body["iccid"],
            payment_method_id=body["paymentMethodId"],
            cfs_service_id=body.get("cfsServiceId", ""),
            price_snapshot=body.get("priceSnapshot"),
        )

    async def on_service_order_failed(session: AsyncSession, body: dict) -> None:
        svc = _build_service(app, session)
        await svc.handle_service_order_failed(
            commercial_order_id=body["commercialOrderId"],
            reason=body.get("reason", ""),
        )

    await bind_consumer(
        channel=channel,
        exchange=exchange,
        retry_exchange=retry_exchange,
        queue_name="com.service_order.completed",
        routing_key="service_order.completed",
        handler=on_service_order_completed,
        session_factory=app.state.session_factory,
        inbox_schema=INBOX_SCHEMA,
        max_retries=max_retries,
        retry_backoff_ms=retry_backoff_ms,
    )
    await bind_consumer(
        channel=channel,
        exchange=exchange,
        retry_exchange=retry_exchange,
        queue_name="com.service_order.failed",
        routing_key="service_order.failed",
        handler=on_service_order_failed,
        session_factory=app.state.session_factory,
        inbox_schema=INBOX_SCHEMA,
        max_retries=max_retries,
        retry_backoff_ms=retry_backoff_ms,
    )
