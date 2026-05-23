"""Transactional outbox relay (v1.2).

The doctrine since v0.1 has been "write the ``audit.domain_event`` row in the
same transaction as the domain write; publish to RabbitMQ after commit
(simplified outbox)". v1.1 and earlier published *inline* inside ``publish()``
— **before** the caller's commit — which meant (a) a rollback after publish
emitted a phantom event, and (b) a publish failure was logged and lost forever
because nothing ever retried the ``published_to_mq = false`` row.

This relay closes both. Publishers now only *stage* the event row (no inline
publish). This relay is the **only** thing that calls ``exchange.publish()``:
a background tick loop that drains unpublished rows in ``occurred_at`` order,
publishes each with the durable ``event_id`` as the AMQP ``message_id`` (the
key the inbox dedups on), and marks the row published — all under
``FOR UPDATE SKIP LOCKED`` so multiple service replicas relay safely.

Delivery is at-least-once: if publish succeeds but the marking commit fails,
the row is re-published next tick. Consumers dedup on ``message_id`` (see
``bss_events.consumer``). The ``ix_domain_event_unpublished`` partial index
(migration 0001) is what makes the drain query cheap.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import aio_pika
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

log = structlog.get_logger()

EXCHANGE_NAME = "bss.events"

_DRAIN_SQL = text(
    """
    SELECT id, event_id, event_type, payload
    FROM audit.domain_event
    WHERE NOT published_to_mq
    ORDER BY occurred_at ASC, id ASC
    LIMIT :batch
    FOR UPDATE SKIP LOCKED
    """
)
_MARK_OK_SQL = text(
    """
    UPDATE audit.domain_event
    SET published_to_mq = true, published_attempts = published_attempts + 1
    WHERE id = :id
    """
)
_MARK_FAIL_SQL = text(
    """
    UPDATE audit.domain_event
    SET published_attempts = published_attempts + 1, last_publish_error = :err
    WHERE id = :id
    """
)


@dataclass
class Relay:
    """Handle for a running relay — stored on ``app.state.outbox_relay``."""

    connection: aio_pika.abc.AbstractRobustConnection
    task: asyncio.Task

    async def stop(self) -> None:
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass
        try:
            await self.connection.close()
        except Exception:
            log.warning("outbox.relay.close_failed", exc_info=True)


async def _drain_once(
    session_factory: async_sessionmaker,
    exchange: aio_pika.abc.AbstractExchange,
    batch_size: int,
) -> int:
    """Publish one batch of unpublished events. Returns the number drained."""
    async with session_factory() as session:
        rows = (await session.execute(_DRAIN_SQL, {"batch": batch_size})).mappings().all()
        for r in rows:
            try:
                msg = aio_pika.Message(
                    body=json.dumps(r["payload"] or {}).encode(),
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    message_id=str(r["event_id"]),
                )
                await exchange.publish(msg, routing_key=r["event_type"])
                await session.execute(_MARK_OK_SQL, {"id": r["id"]})
            except Exception as exc:  # noqa: BLE001 — record + retry next tick
                log.warning(
                    "outbox.relay.publish_failed",
                    event_id=str(r["event_id"]),
                    event_type=r["event_type"],
                    error=str(exc),
                )
                await session.execute(
                    _MARK_FAIL_SQL, {"id": r["id"], "err": str(exc)[:500]}
                )
        await session.commit()
        return len(rows)


async def _run(
    session_factory: async_sessionmaker,
    exchange: aio_pika.abc.AbstractExchange,
    interval_ms: int,
    batch_size: int,
) -> None:
    interval = interval_ms / 1000.0
    while True:
        try:
            drained = await _drain_once(session_factory, exchange, batch_size)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a tick failure must not kill the loop
            log.warning("outbox.relay.tick_failed", exc_info=True)
            drained = 0
        # Back off only when idle; when a batch was full there may be more.
        await asyncio.sleep(0 if drained >= batch_size else interval)


async def start_relay(
    *,
    mq_url: str | None,
    session_factory: async_sessionmaker,
    interval_ms: int = 250,
    batch_size: int = 100,
) -> Relay | None:
    """Start the outbox relay as a background task.

    Returns a ``Relay`` handle (store it on ``app.state.outbox_relay`` and call
    ``.stop()`` in lifespan teardown), or ``None`` when ``mq_url`` is unset
    (event delivery is then off, exactly like the consumers — the durable
    audit log still records everything for later replay).
    """
    if not mq_url:
        log.warning("outbox.relay.not_configured")
        return None

    connection = await aio_pika.connect_robust(mq_url)
    channel = await connection.channel()
    exchange = await channel.declare_exchange(
        EXCHANGE_NAME, aio_pika.ExchangeType.TOPIC, durable=True
    )
    task = asyncio.create_task(_run(session_factory, exchange, interval_ms, batch_size))
    log.info("outbox.relay.started", interval_ms=interval_ms, batch_size=batch_size)
    return Relay(connection=connection, task=task)
