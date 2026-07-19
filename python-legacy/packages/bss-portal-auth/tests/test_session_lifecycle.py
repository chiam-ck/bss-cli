"""current_session, rotate_if_due, revoke_session, link_to_customer."""

from __future__ import annotations

from datetime import timedelta

import pytest
from bss_clock import advance, freeze
from bss_models import Identity, Session
from bss_portal_auth import (
    SessionView,
    current_session,
    link_to_customer,
    revoke_session,
    rotate_if_due,
)
from bss_portal_auth.test_helpers import create_test_session
from sqlalchemy import select


@pytest.mark.asyncio
async def test_current_session_returns_session_and_identity(db_session):
    sess, identity = await create_test_session(db_session, email="ada@x.sg")
    pair = await current_session(db_session, sess.id)
    assert pair is not None
    s, i = pair
    assert s.id == sess.id
    assert i.id == identity.id


@pytest.mark.asyncio
async def test_current_session_bumps_last_seen_at(db_session):
    freeze()
    sess, _ = await create_test_session(db_session, email="ada@x.sg")
    advance(timedelta(minutes=10))

    pair = await current_session(db_session, sess.id)
    assert pair is not None
    s, _i = pair
    assert s.last_seen_at > sess.last_seen_at


@pytest.mark.asyncio
async def test_current_session_returns_none_for_unknown_cookie(db_session):
    assert await current_session(db_session, "no-such-cookie") is None


@pytest.mark.asyncio
async def test_current_session_returns_none_for_revoked(db_session):
    sess, _ = await create_test_session(db_session, email="ada@x.sg")
    await revoke_session(db_session, sess.id)
    assert await current_session(db_session, sess.id) is None


@pytest.mark.asyncio
async def test_current_session_returns_none_for_expired(db_session):
    freeze()
    sess, _ = await create_test_session(db_session, email="ada@x.sg")
    advance(timedelta(hours=25))  # default TTL is 24h
    assert await current_session(db_session, sess.id) is None


@pytest.mark.asyncio
async def test_revoke_session_is_idempotent(db_session):
    sess, _ = await create_test_session(db_session, email="ada@x.sg")
    await revoke_session(db_session, sess.id)
    await revoke_session(db_session, sess.id)  # no raise
    assert await current_session(db_session, sess.id) is None


@pytest.mark.asyncio
async def test_rotate_if_due_does_nothing_when_session_is_fresh(db_session):
    sess, _ = await create_test_session(db_session, email="ada@x.sg")
    assert await rotate_if_due(db_session, sess.id) is None


@pytest.mark.asyncio
async def test_rotate_if_due_mints_new_session_past_half_ttl(db_session):
    freeze()
    sess, _ = await create_test_session(db_session, email="ada@x.sg")
    advance(timedelta(hours=13))  # past half of 24h

    rotated = await rotate_if_due(db_session, sess.id)
    assert isinstance(rotated, SessionView)
    assert rotated.id != sess.id
    assert rotated.identity_id == sess.identity_id

    # Old session is revoked.
    old = (
        await db_session.execute(select(Session).where(Session.id == sess.id))
    ).scalar_one()
    assert old.revoked_at is not None


@pytest.mark.asyncio
async def test_link_to_customer_attaches_customer_id(db_session):
    _sess, identity = await create_test_session(db_session, email="ada@x.sg")
    view = await link_to_customer(
        db_session, identity_id=identity.id, customer_id="CUST-042"
    )
    assert view.customer_id == "CUST-042"
    assert view.status == "registered"

    row = (
        await db_session.execute(select(Identity).where(Identity.id == identity.id))
    ).scalar_one()
    assert row.customer_id == "CUST-042"


@pytest.mark.asyncio
async def test_link_to_customer_idempotent_on_same_customer(db_session):
    _sess, identity = await create_test_session(db_session, email="ada@x.sg")
    await link_to_customer(db_session, identity_id=identity.id, customer_id="CUST-042")
    again = await link_to_customer(
        db_session, identity_id=identity.id, customer_id="CUST-042"
    )
    assert again.customer_id == "CUST-042"


@pytest.mark.asyncio
async def test_link_to_customer_rejects_relink_to_different_customer(db_session):
    _sess, identity = await create_test_session(db_session, email="ada@x.sg")
    await link_to_customer(db_session, identity_id=identity.id, customer_id="CUST-042")
    with pytest.raises(ValueError, match="already linked"):
        await link_to_customer(
            db_session, identity_id=identity.id, customer_id="CUST-099"
        )


@pytest.mark.asyncio
async def test_link_to_customer_unknown_identity_raises(db_session):
    with pytest.raises(ValueError, match="unknown identity"):
        await link_to_customer(db_session, identity_id="IDN-nope", customer_id="CUST-1")
