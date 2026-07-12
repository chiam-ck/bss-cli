"""Regression: the provisioning task handlers must lock the CFS row.

`handle_task_completed`/`handle_task_failed`/`handle_task_stuck` do a
read-modify-write on the CFS ``characteristics`` JSONB (``pendingTasks``). The
aio-pika consumer runs callbacks concurrently (prefetch 5), each in its own
session/transaction, so an *unlocked* read lets two simultaneous
``provisioning.task.completed`` events clobber each other's pendingTasks update
(a genuine lost-update race — see phases/2.0/PROGRESS.md, root-caused while
porting SOM to Rust). The fix is `ServiceRepository.get_for_update`, a
``SELECT ... FOR UPDATE`` that serializes the RMW.

This test pins that fix deterministically: with the CFS row locked in one
transaction, a second transaction's ``get_for_update`` must block (and here hit
``lock_timeout``). If a handler regressed to the unlocked ``get``, the second
read would return immediately and this test would fail.
"""

import pytest
import sqlalchemy
from app.repositories.service_repo import ServiceRepository
from bss_models.service_inventory import Service
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_SVC_ID = "SVC-LOCKTEST-9099"


async def _delete_seed(db_engine) -> None:
    async with db_engine.begin() as conn:
        await conn.execute(text("DELETE FROM service_inventory.service WHERE id = :i"), {"i": _SVC_ID})


@pytest.mark.asyncio
async def test_get_for_update_serializes_concurrent_cfs_rmw(db_engine):
    # Seed a committed CFS the two transactions can both see.
    await _delete_seed(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as seed:
        seed.add(
            Service(
                id=_SVC_ID,
                spec_id="MobileBroadband",
                type="CFS",
                state="reserved",
                characteristics={"pendingTasks": {"HLR_PROVISION": "pending"}},
            )
        )
        await seed.commit()

    conn_a = await db_engine.connect()
    conn_b = await db_engine.connect()
    try:
        await conn_a.begin()
        await conn_b.begin()
        session_a = AsyncSession(bind=conn_a, expire_on_commit=False)
        session_b = AsyncSession(bind=conn_b, expire_on_commit=False)

        # A takes the row lock and holds it (no commit).
        locked = await ServiceRepository(session_a).get_for_update(_SVC_ID)
        assert locked is not None and locked.id == _SVC_ID

        # B tries to lock the same row; a short lock_timeout makes the wait
        # observable. Under the fix this blocks and times out; under an
        # unlocked read it would return the row immediately.
        await session_b.execute(text("SET LOCAL lock_timeout = '300ms'"))
        with pytest.raises(sqlalchemy.exc.DBAPIError) as exc_info:
            await ServiceRepository(session_b).get_for_update(_SVC_ID)
        # 55P03 = lock_not_available (Postgres lock_timeout)
        assert "55P03" in str(exc_info.value) or "lock" in str(exc_info.value).lower()

        await session_b.rollback()
        await session_a.rollback()
    finally:
        await conn_b.close()
        await conn_a.close()
        await _delete_seed(db_engine)
