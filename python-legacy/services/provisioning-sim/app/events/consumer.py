"""RabbitMQ consumer for provisioning.task.created events."""

import json

import aio_pika
import structlog

from app.domain.worker import process_task
from app.repositories.fault_repo import FaultRepository
from app.repositories.task_repo import TaskRepository

log = structlog.get_logger()


async def setup_consumer(app) -> None:
    """Set up MQ consumer in lifespan."""
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
    queue = await channel.declare_queue("provisioning-sim.task.created", durable=True)
    await queue.bind(exchange, "provisioning.task.created")

    app.state.mq_connection = connection
    app.state.mq_exchange = exchange

    async def on_task_created(message: aio_pika.abc.AbstractIncomingMessage):
        async with message.process():
            body = json.loads(message.body)
            log.info(
                "mq.task_created.received",
                service_id=body.get("serviceId"),
                task_type=body.get("taskType"),
            )
            async with app.state.session_factory() as session:
                task_repo = TaskRepository(session)
                fault_repo = FaultRepository(session)
                await process_task(
                    service_id=body["serviceId"],
                    service_order_id=body["serviceOrderId"],
                    commercial_order_id=body.get("commercialOrderId", ""),
                    task_type=body["taskType"],
                    payload=body.get("payload", {}),
                    session=session,
                    task_repo=task_repo,
                    fault_repo=fault_repo,
                    exchange=exchange,
                    esim_provider=app.state.esim_provider,
                )

    await queue.consume(on_task_created)
    log.info("mq.consumer.started", queue="provisioning-sim.task.created")
