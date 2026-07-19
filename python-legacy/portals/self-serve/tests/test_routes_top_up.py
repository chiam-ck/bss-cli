"""v0.10 — VAS top-up at /top-up.

V0_10_0.md Track 3 + Track 10:

* GET /top-up?subscription=SUB-X for owner → lists VAS offerings.
* GET cross-customer → 403 + customer-facing copy (no leak of "exists
  but not yours" vs "does not exist").
* GET ?context=blocked → pre-selects the first data VAS.
* POST /top-up requires step-up; bounces to /auth/step-up if missing.
* POST with grant + ownership → calls purchase_vas, writes a success
  portal_action row, redirects to /top-up/success.
* POST cross-customer → 403, writes a failure portal_action row.
* POST PolicyViolation → 422 + customer-facing copy + audit row with
  the structured rule.
* GET /top-up/success rechecks ownership.
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


def _seed_subscription(fake_clients, sub_id: str, customer_id: str, state: str = "active"):
    sub = {
        "id": sub_id,
        "customerId": customer_id,
        "offeringId": "PLAN_M",
        "msisdn": "91234567",
        "iccid": "8910010000000000123",
        "state": state,
        "stateReason": None,
        "currentPeriodEnd": "2026-05-27T00:00:00+00:00",
        "nextRenewalAt": "2026-05-27T00:00:00+00:00",
        "terminatedAt": None,
        "balances": [],
        "pendingOfferingId": None,
        "pendingEffectiveAt": None,
    }
    fake_clients.subscription.records[sub_id] = sub
    fake_clients.subscription.by_customer.setdefault(customer_id, []).append(sub_id)
    return sub


async def _setup(seed_db, customer_id="CUST-042", *, with_grant_for=None):
    """Mint a session for ``ada@example.sg`` linked to ``customer_id``.

    If ``with_grant_for`` is set, also insert a step_up_grant for that
    action label so the POST request can pass requires_step_up.
    """
    async with seed_db() as db:
        sess, identity = await create_test_session(
            db, email="ada@example.sg", customer_id=customer_id, verified=True
        )
        await db.commit()
        sid = sess.id
        iid = identity.id

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


# ── GET /top-up: list VAS offerings ──────────────────────────────────────


@pytest.mark.asyncio
async def test_get_top_up_lists_vas_offerings_for_owner(seed_db, fake_clients):
    sid, _, _ = await _setup(seed_db)
    _seed_subscription(fake_clients, "SUB-001", "CUST-042")

    with patch(
        "bss_self_serve.routes.top_up.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/top-up?subscription=SUB-001")
            assert resp.status_code == 200
            body = resp.text
            for vid in ("VAS_DATA_1GB", "VAS_DATA_5GB", "VAS_UNLIMITED_DAY"):
                assert vid in body
            assert "Pick a top-up" in body


@pytest.mark.asyncio
async def test_get_top_up_with_blocked_context_preselects_data_vas(
    seed_db, fake_clients
):
    sid, _, _ = await _setup(seed_db)
    _seed_subscription(fake_clients, "SUB-001", "CUST-042", state="blocked")

    with patch(
        "bss_self_serve.routes.top_up.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/top-up?subscription=SUB-001&context=blocked")
            assert resp.status_code == 200
            body = resp.text
            # The blocked-context lead copy.
            assert "Your line is blocked" in body
            # First data VAS is pre-selected (CSS class on the card).
            assert "top-up-vas-card--preselected" in body
            assert "VAS_DATA_1GB" in body


@pytest.mark.asyncio
async def test_get_top_up_returns_403_for_cross_customer(seed_db, fake_clients):
    """Bob can't load top-up for Ada's line."""
    sid, _, _ = await _setup(seed_db, customer_id="CUST-BOB")
    # Subscription belongs to CUST-042, not CUST-BOB.
    _seed_subscription(fake_clients, "SUB-001", "CUST-042")

    with patch(
        "bss_self_serve.routes.top_up.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/top-up?subscription=SUB-001")
            assert resp.status_code == 403
            # Customer-facing copy from error_messages.py.
            assert "belong to your account" in resp.text


# ── POST /top-up requires step-up ────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_top_up_redirects_to_step_up_when_no_grant(seed_db, fake_clients):
    sid, _, _ = await _setup(seed_db, with_grant_for=None)
    _seed_subscription(fake_clients, "SUB-001", "CUST-042")

    with patch(
        "bss_self_serve.routes.top_up.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.post(
                "/top-up?subscription=SUB-001",
                data={"vas_offering_id": "VAS_DATA_1GB"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            loc = resp.headers["location"]
            assert loc.startswith("/auth/step-up")
            assert "action=vas_purchase" in loc
            # No write yet — purchase_vas not called.
            assert fake_clients.subscription.purchase_vas_calls == []


# ── POST /top-up: happy path ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_top_up_calls_purchase_vas_and_audits_success(
    seed_db, fake_clients
):
    sid, _, grant = await _setup(seed_db, with_grant_for="vas_purchase")
    _seed_subscription(fake_clients, "SUB-001", "CUST-042")

    with patch(
        "bss_self_serve.routes.top_up.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            c.cookies.set("bss_portal_step_up", grant)
            resp = c.post(
                "/top-up?subscription=SUB-001",
                data={"vas_offering_id": "VAS_DATA_1GB"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert resp.headers["location"] == (
                "/top-up/success?subscription=SUB-001&vas=VAS_DATA_1GB"
            )

    # purchase_vas was called exactly once with the right args.
    assert fake_clients.subscription.purchase_vas_calls == [
        ("SUB-001", "VAS_DATA_1GB")
    ]

    # portal_action row written: success=True, step_up_consumed=True.
    rows = await _portal_actions(seed_db)
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "vas_purchase"
    assert row.route == "/top-up"
    assert row.method == "POST"
    assert row.success is True
    assert row.step_up_consumed is True
    assert row.error_rule is None
    assert row.customer_id == "CUST-042"


# ── POST /top-up: cross-customer attempt ─────────────────────────────────


@pytest.mark.asyncio
async def test_post_top_up_cross_customer_returns_403_and_audits_failure(
    seed_db, fake_clients
):
    """Bob attempts to top up Ada's line — 403 + failure audit row."""
    sid, _, grant = await _setup(
        seed_db, customer_id="CUST-BOB", with_grant_for="vas_purchase"
    )
    _seed_subscription(fake_clients, "SUB-001", "CUST-042")

    with patch(
        "bss_self_serve.routes.top_up.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            c.cookies.set("bss_portal_step_up", grant)
            resp = c.post(
                "/top-up?subscription=SUB-001",
                data={"vas_offering_id": "VAS_DATA_1GB"},
                follow_redirects=False,
            )
            assert resp.status_code == 403
            assert "belong to your account" in resp.text

    # No purchase_vas call.
    assert fake_clients.subscription.purchase_vas_calls == []

    # Failure audit row.
    rows = await _portal_actions(seed_db)
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "vas_purchase"
    assert row.success is False
    assert row.error_rule == "policy.ownership.subscription_not_owned"
    assert row.customer_id == "CUST-BOB"


# ── POST /top-up: server-side PolicyViolation ────────────────────────────


@pytest.mark.asyncio
async def test_post_top_up_renders_policy_violation_with_known_rule(
    seed_db, fake_clients
):
    """A PolicyViolationFromServer becomes a 422 with customer-facing copy."""
    sid, _, grant = await _setup(seed_db, with_grant_for="vas_purchase")
    _seed_subscription(fake_clients, "SUB-001", "CUST-042")
    fake_clients.subscription.next_error = PolicyViolationFromServer(
        rule="policy.subscription.purchase_vas.subscription_not_active",
        message="engineer-shaped diagnostic",
    )

    with patch(
        "bss_self_serve.routes.top_up.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            c.cookies.set("bss_portal_step_up", grant)
            resp = c.post(
                "/top-up?subscription=SUB-001",
                data={"vas_offering_id": "VAS_DATA_1GB"},
            )
            assert resp.status_code == 422
            # Customer-facing copy from error_messages.py — never the
            # engineer-shaped diagnostic from PolicyViolation.message.
            # apostrophe is HTML-escaped — match unescaped suffix
            assert "active right now" in resp.text
            assert "engineer-shaped diagnostic" not in resp.text

    rows = await _portal_actions(seed_db)
    assert len(rows) == 1
    assert rows[0].success is False
    assert (
        rows[0].error_rule
        == "policy.subscription.purchase_vas.subscription_not_active"
    )


# ── GET /top-up/success: ownership re-checked ────────────────────────────


@pytest.mark.asyncio
async def test_get_top_up_success_renders_for_owner(seed_db, fake_clients):
    sid, _, _ = await _setup(seed_db)
    _seed_subscription(fake_clients, "SUB-001", "CUST-042")
    fake_clients.subscription.balances["SUB-001"] = [
        {
            "id": "BAL-data",
            "subscriptionId": "SUB-001",
            "allowanceType": "data",
            "total": 21504,
            "consumed": 3000,
            "remaining": 18504,
            "unit": "mb",
        },
    ]

    with patch(
        "bss_self_serve.routes.top_up.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/top-up/success?subscription=SUB-001&vas=VAS_DATA_1GB")
            assert resp.status_code == 200
            body = resp.text
            assert "Top-up successful" in body
            assert "Data Top-Up 1GB" in body
            # Fresh balance reflected on the success page.
            assert "18504" in body


@pytest.mark.asyncio
async def test_get_top_up_success_returns_403_for_cross_customer(
    seed_db, fake_clients
):
    sid, _, _ = await _setup(seed_db, customer_id="CUST-BOB")
    _seed_subscription(fake_clients, "SUB-001", "CUST-042")

    with patch(
        "bss_self_serve.routes.top_up.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/top-up/success?subscription=SUB-001&vas=VAS_DATA_1GB")
            assert resp.status_code == 403
