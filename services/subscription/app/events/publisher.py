"""Domain event publisher.

v1.2 — stages the audit.domain_event row in the caller's transaction
(published_to_mq = false) and returns. The outbox relay (bss_events.relay) is
the single thing that publishes to RabbitMQ, draining staged rows after commit.
This removes the publish-before-commit hazard and guarantees a failed publish is
retried rather than lost. The `exchange` kwarg is accepted-but-ignored for
call-site compatibility.
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
    exchange=None,  # v1.2 — ignored; the outbox relay is the only publisher.
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
        "event.staged",
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
    )
