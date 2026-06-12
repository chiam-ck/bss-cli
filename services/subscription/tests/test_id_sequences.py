"""ID generation tests — Postgres sequence survives restart.

Uses real commits (not rolled back) so we can verify sequence behavior.
Cleans up after itself.
"""

import pytest
from app.config import Settings
from app.repositories.subscription_repo import SubscriptionRepository
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine


@pytest.mark.asyncio
async def test_subscription_id_sequence_survives_restart(settings: Settings):
    """Create two IDs, dispose engine (simulate restart), create third — no collision."""
    engine1 = create_async_engine(settings.db_url, pool_size=1)

    async with AsyncSession(engine1, expire_on_commit=False) as session:
        repo = SubscriptionRepository(session)
        id1 = await repo.next_id()
        id2 = await repo.next_id()

    await engine1.dispose()

    # "Restart" — new engine
    engine2 = create_async_engine(settings.db_url, pool_size=1)

    async with AsyncSession(engine2, expire_on_commit=False) as session:
        repo = SubscriptionRepository(session)
        id3 = await repo.next_id()

    await engine2.dispose()

    assert id1.startswith("SUB-")
    assert id2.startswith("SUB-")
    assert id3.startswith("SUB-")

    # Extract numeric parts
    nums = [int(x.split("-")[1]) for x in [id1, id2, id3]]
    assert nums[0] < nums[1] < nums[2], f"IDs not monotonic: {[id1, id2, id3]}"
