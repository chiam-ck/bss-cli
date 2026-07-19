"""Event publisher — audit row in same txn, best-effort MQ publish."""

import json
from uuid import uuid4

import aio_pika
import structlog
from bss_clock import now as clock_now
from bss_models.audit import DomainEvent
from bss_telemetry import current_trace_id
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth_context

log = structlog.get_logger()


async def publish(
    session: AsyncSession,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: dict | None = None,
    exchange: aio_pika.abc.AbstractExchange | None = None,
) -> None:
    """Write DomainEvent audit row and best-effort publish to MQ."""
    ctx = auth_context.current()

    event = DomainEvent(
        event_id=uuid4(),
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        occurred_at=clock_now(),
        trace_id=current_trace_id(),
        actor=ctx.actor,
        channel=ctx.channel,
        tenant_id=ctx.tenant,
        service_identity=ctx.service_identity,
        payload=payload,
        schema_version=1,
        published_to_mq=False,
    )
    session.add(event)

    if exchange:
        try:
            msg = aio_pika.Message(
                body=json.dumps(payload or {}).encode(),
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            )
            await exchange.publish(msg, routing_key=event_type)
            event.published_to_mq = True
        except Exception:
            log.warning("mq.publish.failed", event_type=event_type)
