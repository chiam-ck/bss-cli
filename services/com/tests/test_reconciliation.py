"""v1.2 — reconciliation sweeper flags stranded orders.

An order left in_progress past the threshold (a saga that died beyond the retry
budget, a genuinely declined charge mid-flow) must become operator-visible
rather than sitting invisible forever. The sweeper emits order.stuck exactly
once per stranded order and never auto-resolves it.
"""

import uuid
from datetime import timedelta

import pytest
from app.workers.reconciliation import sweep_once
from bss_clock import now as clock_now
from bss_models.audit import DomainEvent
from bss_models.order_mgmt import ProductOrder
from sqlalchemy import select


async def _make_order(session, *, state: str, age_seconds: int, flagged: bool = False):
    order_id = f"ORD-STUCK-{uuid.uuid4().int % 1_000_000}"
    order = ProductOrder(
        id=order_id,
        customer_id="CUST-0001",
        state=state,
        order_date=clock_now() - timedelta(seconds=age_seconds),
        stuck_flagged_at=clock_now() if flagged else None,
    )
    session.add(order)
    await session.flush()
    return order_id


async def _stuck_events(session, order_id: str) -> int:
    rows = (
        await session.execute(
            select(DomainEvent).where(
                DomainEvent.aggregate_id == order_id,
                DomainEvent.event_type == "order.stuck",
            )
        )
    ).scalars().all()
    return len(rows)


@pytest.mark.asyncio
async def test_old_in_progress_order_is_flagged_and_emits_event(client, db_session):
    app = client._transport.app
    order_id = await _make_order(db_session, state="in_progress", age_seconds=3600)

    flagged = await sweep_once(app)

    assert flagged >= 1
    order = await db_session.get(ProductOrder, order_id)
    assert order.stuck_flagged_at is not None
    assert await _stuck_events(db_session, order_id) == 1


@pytest.mark.asyncio
async def test_recent_in_progress_order_is_not_flagged(client, db_session):
    app = client._transport.app
    order_id = await _make_order(db_session, state="in_progress", age_seconds=10)

    await sweep_once(app)

    order = await db_session.get(ProductOrder, order_id)
    assert order.stuck_flagged_at is None
    assert await _stuck_events(db_session, order_id) == 0


@pytest.mark.asyncio
async def test_already_flagged_order_not_re_emitted(client, db_session):
    app = client._transport.app
    order_id = await _make_order(
        db_session, state="in_progress", age_seconds=3600, flagged=True
    )

    await sweep_once(app)

    assert await _stuck_events(db_session, order_id) == 0


@pytest.mark.asyncio
async def test_completed_order_never_flagged(client, db_session):
    app = client._transport.app
    order_id = await _make_order(db_session, state="completed", age_seconds=3600)

    await sweep_once(app)

    order = await db_session.get(ProductOrder, order_id)
    assert order.stuck_flagged_at is None
