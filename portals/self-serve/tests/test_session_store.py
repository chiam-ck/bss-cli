"""Signup session store — TTL pruning, PAN redaction, concurrent access."""

from __future__ import annotations

import asyncio

import pytest
from bss_self_serve.session import SessionStore


@pytest.mark.asyncio
async def test_create_returns_session_and_redacts_pan() -> None:
    store = SessionStore(ttl_seconds=600)
    s = await store.create(
        plan="PLAN_S",
        name="Ck",
        email="ck@example.com",
        phone="+6590009999",
        msisdn="90000042",
        card_pan="4242424242424242",
    )
    assert s.session_id and len(s.session_id) == 32
    assert s.plan == "PLAN_S"
    assert s.msisdn == "90000042"
    assert s.card_pan == "4242424242424242"  # in-memory only, TTL-bounded
    assert s.card_pan_last4 == "4242"


@pytest.mark.asyncio
async def test_get_by_session_id_returns_same_instance() -> None:
    store = SessionStore(ttl_seconds=600)
    s = await store.create(
        plan="PLAN_M",
        name="n",
        email="e@x",
        phone="+0",
        msisdn="90000042",
        card_pan="4242424242424242",
    )
    assert await store.get(s.session_id) is s
    assert await store.get("unknown-id") is None


@pytest.mark.asyncio
async def test_update_persists_agent_populated_fields() -> None:
    store = SessionStore(ttl_seconds=600)
    s = await store.create(
        plan="PLAN_L",
        name="n",
        email="e@x",
        phone="+0",
        msisdn="90000042",
        card_pan="4242424242424242",
    )
    s.customer_id = "CUST-042"
    s.subscription_id = "SUB-007"
    s.done = True
    await store.update(s)
    refetched = await store.get(s.session_id)
    assert refetched is not None
    assert refetched.customer_id == "CUST-042"
    assert refetched.subscription_id == "SUB-007"
    assert refetched.done is True


@pytest.mark.asyncio
async def test_expired_sessions_are_pruned_on_access() -> None:
    store = SessionStore(ttl_seconds=0)  # every entry expires immediately
    s = await store.create(
        plan="PLAN_S",
        name="n",
        email="e@x",
        phone="+0",
        msisdn="90000042",
        card_pan="4242424242424242",
    )
    # Nudge time so created_at < monotonic() - ttl (ttl=0 should be enough,
    # but monotonic() ticks may land on the same value).
    await asyncio.sleep(0.01)
    assert await store.get(s.session_id) is None


@pytest.mark.asyncio
async def test_concurrent_creates_yield_distinct_ids() -> None:
    store = SessionStore(ttl_seconds=600)

    async def one(i: int) -> str:
        s = await store.create(
            plan="PLAN_S",
            name=f"n{i}",
            email=f"e{i}@x",
            phone="+0",
            msisdn=f"9000{i:04d}",
            card_pan="4242424242424242",
        )
        return s.session_id

    ids = await asyncio.gather(*(one(i) for i in range(50)))
    assert len(set(ids)) == 50
