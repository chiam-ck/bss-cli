"""RabbitMQ consumer for COM — service_order.completed + service_order.failed events."""

import json

import aio_pika
import structlog

from app.repositories.order_repo import OrderRepository

log = structlog.get_logger()


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

    app.state.mq_connection = connection
    app.state.mq_exchange = exchange

    # ── Queue: service_order.completed ───────────────────────────────
    q_completed = await channel.declare_queue("com.service_order.completed", durable=True)
    await q_completed.bind(exchange, "service_order.completed")

    async def on_service_order_completed(message: aio_pika.abc.AbstractIncomingMessage):
        async with message.process():
            body = json.loads(message.body)
            log.info(
                "mq.service_order.completed.received",
                commercial_order_id=body.get("commercialOrderId"),
            )
            async with app.state.session_factory() as session:
                from app.services.order_service import OrderService

                repo = OrderRepository(session)
                svc = OrderService(
                    session=session,
                    repo=repo,
                    crm_client=None,
                    catalog_client=None,
                    payment_client=None,
                    som_client=None,
                    subscription_client=app.state.subscription_client,
                    loyalty_client=app.state.loyalty_client,
                    exchange=exchange,
                )
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
                await session.commit()

    await q_completed.consume(on_service_order_completed)
    log.info("mq.consumer.started", queue="com.service_order.completed")

    # ── Queue: service_order.failed ──────────────────────────────────
    q_failed = await channel.declare_queue("com.service_order.failed", durable=True)
    await q_failed.bind(exchange, "service_order.failed")

    async def on_service_order_failed(message: aio_pika.abc.AbstractIncomingMessage):
        async with message.process():
            body = json.loads(message.body)
            log.info(
                "mq.service_order.failed.received",
                commercial_order_id=body.get("commercialOrderId"),
            )
            async with app.state.session_factory() as session:
                from app.services.order_service import OrderService

                repo = OrderRepository(session)
                svc = OrderService(
                    session=session,
                    repo=repo,
                    crm_client=None,
                    catalog_client=None,
                    payment_client=None,
                    som_client=None,
                    subscription_client=None,
                    exchange=exchange,
                )
                await svc.handle_service_order_failed(
                    commercial_order_id=body["commercialOrderId"],
                    reason=body.get("reason", ""),
                )
                await session.commit()

    await q_failed.consume(on_service_order_failed)
    log.info("mq.consumer.started", queue="com.service_order.failed")
