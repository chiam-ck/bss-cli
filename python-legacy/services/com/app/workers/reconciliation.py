"""Reconciliation sweeper (v1.2) — surface stranded orders to the operator.

The outbox relay + safe consumers make the order pipeline retry instead of
drop, but some failures aren't retryable by the machine (a genuinely declined
charge mid-saga, a downstream that stays down past the retry budget and parks).
This in-process tick loop is the human backstop: an order left ``in_progress``
longer than ``order_stuck_threshold_seconds`` gets an ``order.stuck`` event
(once — guarded by ``stuck_flagged_at``) so it shows up on the operator cockpit
instead of sitting invisible forever.

It does NOT auto-cancel or auto-complete — resolving a stuck order is an operator
decision (mirrors the provisioning-sim "stuck" doctrine). Time is read from the
deterministic clock; the threshold is compared against ``order_date`` (set via
``bss_clock.now()``), not the DB-time ``updated_at``, so frozen-clock scenarios
behave predictably.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import structlog
from bss_clock import now as clock_now
from bss_models.order_mgmt import ProductOrder
from sqlalchemy import select

from app.events.publisher import publish

log = structlog.get_logger()


async def sweep_once(app) -> int:
    """Flag orders stuck in_progress past the threshold. Returns count flagged."""
    settings = app.state.settings
    cutoff = clock_now() - timedelta(seconds=settings.order_stuck_threshold_seconds)

    async with app.state.session_factory() as session:
        rows = (
            await session.execute(
                select(ProductOrder).where(
                    ProductOrder.state == "in_progress",
                    ProductOrder.stuck_flagged_at.is_(None),
                    ProductOrder.order_date < cutoff,
                )
            )
        ).scalars().all()

        for order in rows:
            await publish(
                session,
                event_type="order.stuck",
                aggregate_type="ProductOrder",
                aggregate_id=order.id,
                payload={
                    "commercialOrderId": order.id,
                    "customerId": order.customer_id,
                    "state": order.state,
                    "stuckSinceOrderDate": order.order_date.isoformat()
                    if order.order_date
                    else None,
                },
            )
            order.stuck_flagged_at = clock_now()
            log.warning(
                "order.stuck.flagged",
                commercial_order_id=order.id,
                customer_id=order.customer_id,
            )
        await session.commit()
        return len(rows)


async def reconciliation_tick_loop(app, interval_seconds: int) -> None:
    """Background loop — sweep for stuck orders every interval_seconds."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await sweep_once(app)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a tick failure must not kill the loop
            log.warning("reconciliation.tick_failed", exc_info=True)
