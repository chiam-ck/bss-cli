"""v0.10 — cancel a line at /subscription/<id>/cancel.

V0_10_0.md Track 6 + Track 10:

* GET shows the confirmation page enumerating losses (no proration,
  balance discarded, eSIM released, MSISDN released).
* GET cross-customer → 403 + customer-facing copy.
* GET on an already-terminated subscription → "already cancelled" page.
* POST requires step-up; bounces to /auth/step-up if missing.
* POST with grant + ownership → calls terminate(reason='customer_requested'),
  writes a success portal_action, redirects to /cancelled.
* POST cross-customer → 403 + failure audit row.
* POST PolicyViolation (e.g. invalid_state) → 422 with copy + audit row.
* GET /cancelled rechecks ownership.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from bss_clients import PolicyViolationFromServer
from fastapi.testclient import TestClient
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault(
    "BSS_PORTAL_TOKEN_PEPPER",
    "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
)
os.environ.setdefault("BSS_PORTAL_EMAIL_ADAPTER", "noop")
os.environ.setdefault("BSS_PORTAL_EMAIL_PROVIDER", "noop")  # v0.14 — both names handled
os.environ.setdefault("BSS_PORTAL_DEV_INSECURE_COOKIE", "1")

from bss_clock.clock import reset_for_tests as _reset_clock  # noqa: E402
from bss_models import PortalAction  # noqa: E402
from bss_portal_auth.test_helpers import (  # noqa: E402
    create_test_session,
    mint_step_up_grant,
)
from bss_self_serve.config import Settings  # noqa: E402
from bss_self_serve.main import create_app  # noqa: E402
from bss_self_serve.middleware import PORTAL_SESSION_COOKIE  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]


class _DbSettings(BaseSettings):
    BSS_DB_URL: str = ""
    model_config = SettingsConfigDict(
        env_file=_REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@pytest.fixture(autouse=True)
def _clock():
    _reset_clock()
    yield
    _reset_clock()


@pytest.fixture
def db_url() -> str:
    url = _DbSettings().BSS_DB_URL or os.environ.get("BSS_DB_URL", "")
    if not url:
        pytest.fail("BSS_DB_URL is not set. Export it or add to .env.")
    os.environ["BSS_DB_URL"] = url
    return url


@pytest_asyncio.fixture
async def seed_db(db_url: str):
    engine = create_async_engine(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _scrub(s):
        await s.execute(text(
            "TRUNCATE portal_auth.portal_action, portal_auth.login_attempt, "
            "portal_auth.session, portal_auth.login_token, "
            "portal_auth.identity RESTART IDENTITY CASCADE"
        ))

    async with factory() as s:
        await _scrub(s)
        await s.commit()
    yield factory
    async with factory() as s:
        await _scrub(s)
        await s.commit()
    await engine.dispose()


def _seed_subscription(fake_clients, *, sub_id, customer_id, state="active",
                       msisdn="91234567", iccid="8910010000000000123",
                       terminated_at=None):
    sub = {
        "id": sub_id,
        "customerId": customer_id,
        "offeringId": "PLAN_M",
        "msisdn": msisdn,
        "iccid": iccid,
        "state": state,
        "stateReason": None,
        "currentPeriodEnd": "2026-05-27T00:00:00+00:00",
        "nextRenewalAt": "2026-05-27T00:00:00+00:00",
        "terminatedAt": terminated_at,
    }
    fake_clients.subscription.records[sub_id] = sub
    fake_clients.subscription.by_customer.setdefault(customer_id, []).append(sub_id)


async def _setup(seed_db, customer_id="CUST-042", *, with_grant_for=None):
    async with seed_db() as db:
        sess, identity = await create_test_session(
            db, email="ada@example.sg", customer_id=customer_id, verified=True
        )
        await db.commit()
        sid, iid = sess.id, identity.id

    grant = None
    if with_grant_for is not None:
        async with seed_db() as db:
            grant = await mint_step_up_grant(
                db, session_id=sid, action_label=with_grant_for
            )
            await db.commit()
    return sid, iid, grant


async def _portal_actions(seed_db) -> list[PortalAction]:
    async with seed_db() as s:
        rows = (await s.execute(select(PortalAction))).scalars().all()
        return list(rows)


# ── GET: confirmation page ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_confirm_renders_losses_for_owner(seed_db, fake_clients):
    sid, _, _ = await _setup(seed_db)
    _seed_subscription(fake_clients, sub_id="SUB-001", customer_id="CUST-042")
    fake_clients.subscription.balances["SUB-001"] = [
        {
            "id": "BAL-data",
            "subscriptionId": "SUB-001",
            "allowanceType": "data",
            "total": 20480,
            "consumed": 5000,
            "remaining": 15480,
            "unit": "mb",
        },
    ]

    with patch(
        "bss_self_serve.routes.cancel.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/subscription/SUB-001/cancel")
            assert resp.status_code == 200
            body = resp.text
            # Each enumerated loss is present.
            assert "No refund" in body
            assert "Bundle balance is discarded" in body
            assert "eSIM profile is released" in body
            assert "Phone number is released" in body
            # Concrete numbers from the balance + msisdn + iccid last-4.
            assert "15480 / 20480 mb" in body
            assert "91234567" in body
            assert "…0123" in body
            # Submit button present.
            assert "I understand, cancel my line" in body


@pytest.mark.asyncio
async def test_get_confirm_cross_customer_returns_403(seed_db, fake_clients):
    sid, _, _ = await _setup(seed_db, customer_id="CUST-BOB")
    _seed_subscription(fake_clients, sub_id="SUB-001", customer_id="CUST-ADA")

    with patch(
        "bss_self_serve.routes.cancel.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/subscription/SUB-001/cancel")
            assert resp.status_code == 403
            assert "belong to your account" in resp.text


@pytest.mark.asyncio
async def test_get_confirm_already_terminated_short_circuits(seed_db, fake_clients):
    sid, _, _ = await _setup(seed_db)
    _seed_subscription(
        fake_clients, sub_id="SUB-001", customer_id="CUST-042",
        state="terminated", terminated_at="2026-04-20T12:00:00+00:00",
    )

    with patch(
        "bss_self_serve.routes.cancel.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/subscription/SUB-001/cancel")
            assert resp.status_code == 200
            assert "Already cancelled" in resp.text
            # The destructive form is NOT rendered for an already-terminated line.
            assert "I understand, cancel my line" not in resp.text


# ── POST: step-up required ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_redirects_to_step_up_when_no_grant(seed_db, fake_clients):
    sid, _, _ = await _setup(seed_db, with_grant_for=None)
    _seed_subscription(fake_clients, sub_id="SUB-001", customer_id="CUST-042")

    with patch(
        "bss_self_serve.routes.cancel.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.post(
                "/subscription/SUB-001/cancel",
                follow_redirects=False,
            )
            assert resp.status_code == 303
            loc = resp.headers["location"]
            assert "/auth/step-up" in loc
            assert "action=subscription_terminate" in loc
            # No write happened.
            assert fake_clients.subscription.terminate_calls == []


# ── POST: happy path ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_calls_terminate_with_reason_and_audits_success(
    seed_db, fake_clients
):
    sid, _, grant = await _setup(seed_db, with_grant_for="subscription_terminate")
    _seed_subscription(fake_clients, sub_id="SUB-001", customer_id="CUST-042")

    with patch(
        "bss_self_serve.routes.cancel.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            c.cookies.set("bss_portal_step_up", grant)
            resp = c.post(
                "/subscription/SUB-001/cancel",
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert resp.headers["location"] == "/subscription/SUB-001/cancelled"

    # Reason is forensic — must come through verbatim.
    assert fake_clients.subscription.terminate_calls == [
        ("SUB-001", "customer_requested")
    ]

    rows = await _portal_actions(seed_db)
    assert len(rows) == 1
    assert rows[0].action == "subscription_terminate"
    assert rows[0].route == "/subscription/SUB-001/cancel"
    assert rows[0].success is True
    assert rows[0].step_up_consumed is True
    assert rows[0].customer_id == "CUST-042"


# ── POST: cross-customer audit ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_cross_customer_returns_403_and_audits_failure(
    seed_db, fake_clients
):
    sid, _, grant = await _setup(
        seed_db, customer_id="CUST-BOB", with_grant_for="subscription_terminate"
    )
    _seed_subscription(fake_clients, sub_id="SUB-001", customer_id="CUST-ADA")

    with patch(
        "bss_self_serve.routes.cancel.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            c.cookies.set("bss_portal_step_up", grant)
            resp = c.post("/subscription/SUB-001/cancel")
            assert resp.status_code == 403

    assert fake_clients.subscription.terminate_calls == []
    rows = await _portal_actions(seed_db)
    assert rows[0].success is False
    assert rows[0].error_rule == "policy.ownership.subscription_not_owned"


# ── POST: server-side PolicyViolation ────────────────────────────────────


@pytest.mark.asyncio
async def test_post_policy_violation_renders_form_with_error(seed_db, fake_clients):
    sid, _, grant = await _setup(seed_db, with_grant_for="subscription_terminate")
    _seed_subscription(fake_clients, sub_id="SUB-001", customer_id="CUST-042")
    fake_clients.subscription.next_error = PolicyViolationFromServer(
        rule="policy.subscription.terminate.subscription_already_terminated",
        message="engineer-shaped diagnostic",
    )

    with patch(
        "bss_self_serve.routes.cancel.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            c.cookies.set("bss_portal_step_up", grant)
            resp = c.post("/subscription/SUB-001/cancel")
            assert resp.status_code == 422
            assert "already cancelled" in resp.text
            assert "engineer-shaped diagnostic" not in resp.text

    rows = await _portal_actions(seed_db)
    assert rows[0].success is False
    assert (
        rows[0].error_rule
        == "policy.subscription.terminate.subscription_already_terminated"
    )


# ── GET /cancelled — ownership rechecked ─────────────────────────────────


@pytest.mark.asyncio
async def test_get_cancelled_renders_for_owner(seed_db, fake_clients):
    sid, _, _ = await _setup(seed_db)
    _seed_subscription(
        fake_clients, sub_id="SUB-001", customer_id="CUST-042",
        state="terminated", terminated_at="2026-04-27T00:00:00+00:00",
    )

    with patch(
        "bss_self_serve.routes.cancel.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/subscription/SUB-001/cancelled")
            assert resp.status_code == 200
            assert "Line cancelled" in resp.text
            assert "terminated" in resp.text


@pytest.mark.asyncio
async def test_get_cancelled_cross_customer_returns_403(seed_db, fake_clients):
    sid, _, _ = await _setup(seed_db, customer_id="CUST-BOB")
    _seed_subscription(
        fake_clients, sub_id="SUB-001", customer_id="CUST-ADA",
        state="terminated",
    )

    with patch(
        "bss_self_serve.routes.cancel.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/subscription/SUB-001/cancelled")
            assert resp.status_code == 403
