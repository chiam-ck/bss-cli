"""Race-condition regression for ``handle_usage_rated``.

The ``customer_signup_and_exhaust`` hero scenario flaked intermittently
when two ``usage.rated`` events arrived back-to-back for the same
subscription. Even though ``handle_usage_rated`` issues
``SELECT ... FOR UPDATE`` and the lock is acquired correctly at the
DB level, SQLAlchemy returned a CACHED Python object from the
session's identity map instead of the freshly-fetched row. Cache
was populated earlier in the same handler by
``self._repo.get(sub_id)``, which selectinload-eager-loads
``Subscription.balances``.

The fix: ``populate_existing=True`` on the FOR UPDATE statement so
SQLAlchemy overwrites the cached object with the fresh DB read.

This test proves the fix without spinning up a real RabbitMQ +
two concurrent consumers — it reproduces the cache-staleness
mechanism in a single session.
"""

from __future__ import annotations

import pytest
from app.repositories.subscription_repo import SubscriptionRepository
from sqlalchemy import text


@pytest.mark.asyncio
async def test_get_balance_for_update_returns_fresh_db_state_not_cache(
    client, db_session
):
    """If a balance row was loaded via selectinload earlier in the same
    session, ``get_balance_for_update`` MUST still return the latest
    DB-side value — not the cached Python object's stale ``consumed``
    attribute. Without ``populate_existing`` this test would fail with
    consumed==0 (the value cached during the eager load).
    """
    # 1) Create a subscription. ``client.post`` runs through the route
    # which uses its own session, but since conftest's `_fake_commit`
    # downgrades commit to flush within the outer rolled-back txn, the
    # write is visible to db_session.
    create_resp = await client.post(
        "/subscription-api/v1/subscription",
        json={
            "customerId": "CUST-0001",
            "offeringId": "PLAN_M",
            "msisdn": "90000777",
            "iccid": "8910000000000777",
            "paymentMethodId": "PM-0001",
        },
    )
    assert create_resp.status_code == 201, create_resp.text
    sub_id = create_resp.json()["id"]

    repo = SubscriptionRepository(db_session)

    # 2) Eager-load via .get() — this populates the session's identity
    # map with the BundleBalance row at consumed=0 (the load-time value).
    sub = await repo.get(sub_id)
    assert sub is not None
    cached_data_balance = next(b for b in sub.balances if b.allowance_type == "data")
    assert cached_data_balance.consumed == 0

    # 3) Mutate the underlying row via raw SQL. The Python object is now
    # stale relative to the DB. (In production this is what the OTHER
    # concurrent handler's commit looks like to us.)
    await db_session.execute(
        text(
            "UPDATE subscription.bundle_balance "
            "SET consumed = 4096 "
            "WHERE subscription_id = :sid AND allowance_type = 'data'"
        ),
        {"sid": sub_id},
    )

    # 4) Call get_balance_for_update — without populate_existing this
    # would hit the identity map and return the stale Python object
    # (consumed=0). With populate_existing, SQLAlchemy overwrites the
    # cached attributes from the freshly-locked row.
    fresh = await repo.get_balance_for_update(sub_id, "data")
    assert fresh is not None
    assert fresh.consumed == 4096, (
        "get_balance_for_update returned a stale cached value — the "
        "populate_existing=True execution option is missing or broken. "
        "This would cause concurrent usage.rated events to overwrite "
        "each other's decrements; see customer_signup_and_exhaust flake."
    )

    # 5) Bonus: the cached object reference should now also reflect
    # the fresh value (SQLAlchemy reuses the same instance, just with
    # updated attributes).
    assert cached_data_balance.consumed == 4096
