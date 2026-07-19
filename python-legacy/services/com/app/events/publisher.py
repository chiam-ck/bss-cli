"""Event publisher — stages the audit row; the outbox relay delivers it.

v1.2 — this no longer publishes to RabbitMQ inline. It only writes the
``audit.domain_event`` row (``published_to_mq = false``) in the caller's
transaction. ``bss_events.relay`` is the single publisher to RabbitMQ,
draining staged rows after commit. This removes the old publish-before-commit
hazard (a rollback could emit a phantom event) and guarantees a failed publish
is retried instead of lost.

The ``exchange`` kwarg is accepted-but-ignored for call-site compatibility.
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
    """Stage a DomainEvent row in the current transaction (relay delivers it)."""
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
