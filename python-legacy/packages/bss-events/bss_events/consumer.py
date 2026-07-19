"""Safe MQ consumer helper (v1.2).

Before v1.2 every consumer used ``async with message.process():`` with default
args — which nacks ``requeue=False`` on *any* handler exception. No retry, no
dead-letter queue. A single transient error (a DB blip, a downstream 503, the
v1.1.3 exhausted-promo path) dropped the message permanently and stranded the
order. This helper replaces that pattern across COM/SOM with three guarantees:

1. **Idempotent** — an inbox table (``<schema>.processed_event``) is claimed in
   the *same transaction* as the handler's writes, keyed on the relay's
   ``message_id`` (= the durable ``event_id``). A redelivered message whose row
   already exists is acked and skipped, so at-least-once delivery can't double a
   side effect (e.g. a second card-on-file charge).

2. **Retried with backoff** — on handler failure the message dead-letters to a
   per-queue retry queue with a TTL, then comes back. ``x-death`` counts the
   cycles.

3. **Parked, never lost** — after ``max_retries`` the message is moved to a
   ``<queue>.parked`` queue and an ``mq.message.parked`` event is logged. A
   parked message is an operator-visible incident, not a black hole.

Topology per main queue ``q`` bound to ``bss.events`` on ``routing_key``::

    q            --(x-dlx: bss.events.retry, dlrk: q)-->  bss.events.retry
    bss.events.retry  --(direct, key=q)-->  q.retry  (ttl, x-dlx: bss.events, dlrk: routing_key)
    q.retry      --(ttl expires)-->  bss.events  --(routing_key)-->  q   [retry]
    q (>= max)   -->  q.parked   (acked, no further retry)
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

import aio_pika
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()

EXCHANGE_NAME = "bss.events"
RETRY_EXCHANGE_NAME = "bss.events.retry"

# handler(session, body) — does its domain work on the provided session; the
# helper owns commit/rollback and the inbox claim (so they're one transaction).
Handler = Callable[[AsyncSession, dict], Awaitable[None]]


async def declare_retry_exchange(
    channel: aio_pika.abc.AbstractChannel,
) -> aio_pika.abc.AbstractExchange:
    """Declare the shared retry (dead-letter) exchange. Idempotent."""
    return await channel.declare_exchange(
        RETRY_EXCHANGE_NAME, aio_pika.ExchangeType.DIRECT, durable=True
    )


def _death_count(message: aio_pika.abc.AbstractIncomingMessage) -> int:
    """How many times this message has cycled through the retry queue."""
    xdeath = (message.headers or {}).get("x-death")
    if not xdeath:
        return 0
    try:
        return int(xdeath[0].get("count", 0))
    except (KeyError, IndexError, TypeError, ValueError):
        return 0


async def _claim_inbox(
    session: AsyncSession, schema: str, consumer: str, event_id: str
) -> bool:
    """Insert the inbox row in the handler's txn. True if newly claimed."""
    result = await session.execute(
        text(
            f"""
            INSERT INTO {schema}.processed_event (event_id, consumer, processed_at)
            VALUES (:event_id, :consumer, now())
            ON CONFLICT (event_id, consumer) DO NOTHING
            """
        ),
        {"event_id": event_id, "consumer": consumer},
    )
    return result.rowcount == 1


async def bind_consumer(
    *,
    channel: aio_pika.abc.AbstractChannel,
    exchange: aio_pika.abc.AbstractExchange,
    retry_exchange: aio_pika.abc.AbstractExchange,
    queue_name: str,
    routing_key: str,
    handler: Handler,
    session_factory: async_sessionmaker,
    inbox_schema: str,
    max_retries: int = 5,
    retry_backoff_ms: int = 5000,
) -> None:
    """Declare the queue + retry/parked topology and start consuming.

    ``handler(session, body)`` runs its writes on the supplied session; this
    helper claims the inbox row, commits on success, and on failure rolls back
    and routes the message to retry or park. The handler must NOT commit.
    """
    # Main queue dead-letters failures to the retry exchange, keyed by its name.
    main_q = await channel.declare_queue(
        queue_name,
        durable=True,
        arguments={
            "x-dead-letter-exchange": RETRY_EXCHANGE_NAME,
            "x-dead-letter-routing-key": queue_name,
        },
    )
    await main_q.bind(exchange, routing_key)

    # Retry queue: holds the message for the backoff TTL, then dead-letters it
    # back to the main exchange under the ORIGINAL routing key → main queue.
    retry_q = await channel.declare_queue(
        f"{queue_name}.retry",
        durable=True,
        arguments={
            "x-message-ttl": retry_backoff_ms,
            "x-dead-letter-exchange": EXCHANGE_NAME,
            "x-dead-letter-routing-key": routing_key,
        },
    )
    await retry_q.bind(retry_exchange, queue_name)

    # Parked queue: terminal resting place for poison messages.
    parked_q = await channel.declare_queue(f"{queue_name}.parked", durable=True)

    async def on_message(message: aio_pika.abc.AbstractIncomingMessage) -> None:
        body = json.loads(message.body)
        event_id = message.message_id
        try:
            async with session_factory() as session:
                if event_id is not None:
                    claimed = await _claim_inbox(
                        session, inbox_schema, queue_name, event_id
                    )
                    if not claimed:
                        log.info(
                            "inbox.duplicate.skipped",
                            queue=queue_name,
                            event_id=event_id,
                        )
                        await message.ack()
                        return
                await handler(session, body)
                await session.commit()
            await message.ack()
        except Exception as exc:  # noqa: BLE001 — decide retry vs park
            attempts = _death_count(message)
            if attempts >= max_retries:
                # Out of budget — park it, ack the original so it stops cycling.
                await channel.default_exchange.publish(
                    aio_pika.Message(
                        body=message.body,
                        content_type="application/json",
                        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                        message_id=event_id,
                        headers={
                            "parked_reason": str(exc)[:500],
                            "x-death": message.headers.get("x-death") if message.headers else None,
                        },
                    ),
                    routing_key=parked_q.name,
                )
                log.error(
                    "mq.message.parked",
                    queue=queue_name,
                    event_id=event_id,
                    attempts=attempts,
                    error=str(exc),
                )
                await message.ack()
            else:
                # Nack without requeue → dead-letters to the retry queue (TTL),
                # then comes back to the main queue after the backoff.
                log.warning(
                    "mq.message.retry",
                    queue=queue_name,
                    event_id=event_id,
                    attempt=attempts + 1,
                    max_retries=max_retries,
                    error=str(exc),
                )
                await message.nack(requeue=False)

    await main_q.consume(on_message)
    log.info("mq.consumer.started", queue=queue_name, routing_key=routing_key)
