"""Domain event publisher.

Writes to audit.domain_event in the same transaction as the domain write.
RabbitMQ publish is best-effort after commit (simplified outbox).
"""

from uuid import uuid4

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
) -> None:
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
        payload=payload or {},
        schema_version=1,
        published_to_mq=False,
    )
    session.add(event)
    log.info(
        "event.published",
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
    )
