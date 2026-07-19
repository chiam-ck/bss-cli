"""RabbitMQ consumer — Subscription reacts to `usage.rated`."""

import json

import aio_pika
import structlog

from app.repositories.subscription_repo import SubscriptionRepository
from app.repositories.vas_repo import VasPurchaseRepository

log = structlog.get_logger()


async def setup_consumer(app) -> None:
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

    queue = await channel.declare_queue("subscription.usage.rated", durable=True)
    await queue.bind(exchange, "usage.rated")

    async def on_usage_rated(message: aio_pika.abc.AbstractIncomingMessage):
        async with message.process():
            body = json.loads(message.body)
            log.info(
                "mq.usage.rated.received",
                subscription_id=body.get("subscriptionId"),
                allowance_type=body.get("allowanceType"),
                consumed_quantity=body.get("consumedQuantity"),
                usage_event_id=body.get("usageEventId"),
            )
            # Each event gets its own session — the SELECT FOR UPDATE inside
            # handle_usage_rated serializes concurrent decrements for the
            # same (subscription, allowance).
            async with app.state.session_factory() as session:
                from app.services.subscription_service import SubscriptionService

                svc = SubscriptionService(
                    session=session,
                    repo=SubscriptionRepository(session),
                    vas_repo=VasPurchaseRepository(session),
                    crm_client=app.state.crm_client,
                    payment_client=app.state.payment_client,
                    catalog_client=app.state.catalog_client,
                    inventory_client=app.state.inventory_client,
                )
                try:
                    await svc.handle_usage_rated(
                        subscription_id=body["subscriptionId"],
                        allowance_type=body["allowanceType"],
                        consumed_quantity=int(body["consumedQuantity"]),
                        usage_event_id=body.get("usageEventId", ""),
                        exchange=exchange,
                    )
                except Exception:
                    log.exception(
                        "subscription.usage_rated.handler_failed",
                        subscription_id=body.get("subscriptionId"),
                    )
                    await session.rollback()

    await queue.consume(on_usage_rated)
    log.info("mq.consumer.started", queue="subscription.usage.rated")

    # ── v0.7 — LoggingNotificationConsumer ──────────────────────────────
    # Pretty-prints `notification.requested` events to stdout so operators
    # can verify the price-migration notice flow without an SMTP/SES adapter.
    # Real email delivery lands in v1.0.
    notif_queue = await channel.declare_queue(
        "subscription.notification.logger", durable=True
    )
    await notif_queue.bind(exchange, "notification.requested")

    async def on_notification_requested(
        message: aio_pika.abc.AbstractIncomingMessage,
    ):
        async with message.process():
            body = json.loads(message.body)
            log.info(
                "notification.dev_inbox",
                customer_id=body.get("customerId"),
                channel=body.get("channel"),
                template=body.get("template"),
                template_args=body.get("templateArgs"),
            )

    await notif_queue.consume(on_notification_requested)
    log.info("mq.consumer.started", queue="subscription.notification.logger")
