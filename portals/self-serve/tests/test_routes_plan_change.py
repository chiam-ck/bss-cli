"""v0.10 PR 10 — plan change at /plan/change.

V0_10_0.md Track 9 + Track 10. Schedule a plan switch via the v0.7
machinery (one direct call to subscription.schedule_plan_change),
and cancel a pending switch (one direct call to
subscription.cancel_plan_change). Both gated by step-up + ownership.

The doctrine load-bearing assertion: the confirmation page MUST
say "No proration" explicitly. V0_10_0.md "Confirmation page
explicit on no proration" — turning the abstract motto principle
into a sentence the customer reads.
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
    async with factory() as s:
        await s.execute(text(
            "TRUNCATE portal_auth.portal_action, portal_auth.login_attempt, "
            "portal_auth.session, portal_auth.login_token, "
            "portal_auth.identity RESTART IDENTITY CASCADE"
        ))
        await s.commit()
    yield factory
    async with factory() as s:
        await s.execute(text(
            "TRUNCATE portal_auth.portal_action, portal_auth.login_attempt, "
            "portal_auth.session, portal_auth.login_token, "
            "portal_auth.identity RESTART IDENTITY CASCADE"
        ))
        await s.commit()
    await engine.dispose()


def _seed_subscription(
    fake_clients,
    *,
    sub_id="SUB-001",
    customer_id="CUST-042",
    offering_id="PLAN_M",
    pending_offering_id=None,
):
    sub = {
        "id": sub_id,
        "customerId": customer_id,
        "offeringId": offering_id,
        "msisdn": "91234567",
        "iccid": "8910010000000000123",
        "state": "active",
        "stateReason": None,
        "currentPeriodEnd": "2026-05-27T00:00:00+00:00",
        "nextRenewalAt": "2026-05-27T00:00:00+00:00",
        "terminatedAt": None,
        "pendingOfferingId": pending_offering_id,
        "pendingEffectiveAt": (
            "2026-05-27T00:00:00+00:00" if pending_offering_id else None
        ),
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
        return list((await s.execute(select(PortalAction))).scalars().all())


# ── GET /plan/change ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_lists_offerings_with_current_plan_disabled(seed_db, fake_clients):
    sid, _, _ = await _setup(seed_db)
    _seed_subscription(fake_clients, offering_id="PLAN_M")

    with patch(
        "bss_self_serve.routes.plan_change.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/plan/change?subscription=SUB-001")
            assert resp.status_code == 200
            body = resp.text

            for plan_id in ("PLAN_S", "PLAN_M", "PLAN_L"):
                assert plan_id in body
            # Current-plan badge sits next to PLAN_M, no Switch button for it.
            assert "Current plan" in body
            assert "plan-change-card--current" in body
            # Switch button only on the non-current plans.
            assert 'value="PLAN_S"' in body
            assert 'value="PLAN_L"' in body
            # Doctrine: "No proration" mentioned on the form too.
            assert "No proration" in body


@pytest.mark.asyncio
async def test_get_renders_pending_banner_with_cancel_form(seed_db, fake_clients):
    sid, _, _ = await _setup(seed_db)
    _seed_subscription(
        fake_clients, offering_id="PLAN_M", pending_offering_id="PLAN_L"
    )

    with patch(
        "bss_self_serve.routes.plan_change.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/plan/change?subscription=SUB-001")
            body = resp.text
            assert "plan-change-pending" in body
            assert "PLAN_L" in body
            assert 'action="/plan/change/cancel"' in body
            # Pending plan card has its own badge + no submit button.
            assert "plan-change-card--pending" in body


@pytest.mark.asyncio
async def test_get_cross_customer_returns_403(seed_db, fake_clients):
    sid, _, _ = await _setup(seed_db, customer_id="CUST-BOB")
    _seed_subscription(fake_clients, customer_id="CUST-ADA")

    with patch(
        "bss_self_serve.routes.plan_change.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/plan/change?subscription=SUB-001")
            assert resp.status_code == 403


# ── POST /plan/change — schedule ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_schedule_requires_step_up(seed_db, fake_clients):
    sid, _, _ = await _setup(seed_db)
    _seed_subscription(fake_clients)

    with patch(
        "bss_self_serve.routes.plan_change.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.post(
                "/plan/change?subscription=SUB-001",
                data={"new_offering_id": "PLAN_L"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "/auth/step-up" in resp.headers["location"]
            assert fake_clients.subscription.schedule_plan_change_calls == []


@pytest.mark.asyncio
async def test_post_schedule_succeeds_and_redirects_to_confirmation(
    seed_db, fake_clients
):
    sid, _, grant = await _setup(seed_db, with_grant_for="plan_change_schedule")
    _seed_subscription(fake_clients, offering_id="PLAN_M")

    with patch(
        "bss_self_serve.routes.plan_change.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            c.cookies.set("bss_portal_step_up", grant)
            resp = c.post(
                "/plan/change?subscription=SUB-001",
                data={"new_offering_id": "PLAN_L"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            loc = resp.headers["location"]
            assert loc.startswith("/plan/change/scheduled")
            assert "subscription=SUB-001" in loc
            assert "new_offering=PLAN_L" in loc

    assert fake_clients.subscription.schedule_plan_change_calls == [
        ("SUB-001", "PLAN_L")
    ]
    rows = await _portal_actions(seed_db)
    assert rows[0].action == "plan_change_schedule"
    assert rows[0].success is True


@pytest.mark.asyncio
async def test_post_schedule_cross_customer_returns_403(seed_db, fake_clients):
    sid, _, grant = await _setup(
        seed_db, customer_id="CUST-BOB", with_grant_for="plan_change_schedule"
    )
    _seed_subscription(fake_clients, customer_id="CUST-ADA")

    with patch(
        "bss_self_serve.routes.plan_change.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            c.cookies.set("bss_portal_step_up", grant)
            resp = c.post(
                "/plan/change?subscription=SUB-001",
                data={"new_offering_id": "PLAN_L"},
            )
            assert resp.status_code == 403

    assert fake_clients.subscription.schedule_plan_change_calls == []
    rows = await _portal_actions(seed_db)
    assert rows[0].success is False


@pytest.mark.asyncio
async def test_post_schedule_policy_violation_renders_form_with_error(
    seed_db, fake_clients
):
    sid, _, grant = await _setup(seed_db, with_grant_for="plan_change_schedule")
    _seed_subscription(fake_clients, offering_id="PLAN_M")
    fake_clients.subscription.next_error = PolicyViolationFromServer(
        rule="policy.subscription.plan_change.target_not_sellable_now",
        message="engineer-shaped",
    )

    with patch(
        "bss_self_serve.routes.plan_change.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            c.cookies.set("bss_portal_step_up", grant)
            resp = c.post(
                "/plan/change?subscription=SUB-001",
                data={"new_offering_id": "PLAN_L"},
            )
            assert resp.status_code == 422
            assert "available right now" in resp.text
            assert "engineer-shaped" not in resp.text


# ── GET /plan/change/scheduled — confirmation, "No proration" ────────────


@pytest.mark.asyncio
async def test_get_scheduled_says_no_proration_explicitly(seed_db, fake_clients):
    """V0_10_0.md "Confirmation page explicit on no proration." This is
    where the abstract motto becomes a sentence the customer reads."""
    sid, _, _ = await _setup(seed_db)
    _seed_subscription(fake_clients, pending_offering_id="PLAN_L")

    with patch(
        "bss_self_serve.routes.plan_change.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get(
                "/plan/change/scheduled"
                "?subscription=SUB-001&new_offering=PLAN_L"
                "&effective_at=2026-05-27T00:00:00+00:00"
            )
            assert resp.status_code == 200
            body = resp.text
            # Doctrine assertions.
            assert "No proration" in body
            assert "pro-rate" in body or "pro-rated" in body or "pro-rate the" in body
            # Concrete dates + new plan name resolved from catalog.
            assert "Long Haul" in body
            assert "2026-05-27" in body


@pytest.mark.asyncio
async def test_get_scheduled_cross_customer_returns_403(seed_db, fake_clients):
    sid, _, _ = await _setup(seed_db, customer_id="CUST-BOB")
    _seed_subscription(fake_clients, customer_id="CUST-ADA")

    with patch(
        "bss_self_serve.routes.plan_change.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get(
                "/plan/change/scheduled?subscription=SUB-001&new_offering=PLAN_L"
            )
            assert resp.status_code == 403


# ── POST /plan/change/cancel ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_cancel_pending_succeeds_and_audits(seed_db, fake_clients):
    sid, _, grant = await _setup(seed_db, with_grant_for="plan_change_cancel")
    _seed_subscription(fake_clients, pending_offering_id="PLAN_L")

    with patch(
        "bss_self_serve.routes.plan_change.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            c.cookies.set("bss_portal_step_up", grant)
            resp = c.post(
                "/plan/change/cancel",
                data={"subscription_id": "SUB-001"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert resp.headers["location"] == "/?flash=plan_change_cancelled"

    assert fake_clients.subscription.cancel_plan_change_calls == ["SUB-001"]
    rows = await _portal_actions(seed_db)
    assert rows[0].action == "plan_change_cancel"
    assert rows[0].success is True


@pytest.mark.asyncio
async def test_post_cancel_cross_customer_returns_403(seed_db, fake_clients):
    sid, _, grant = await _setup(
        seed_db, customer_id="CUST-BOB", with_grant_for="plan_change_cancel"
    )
    _seed_subscription(fake_clients, customer_id="CUST-ADA")

    with patch(
        "bss_self_serve.routes.plan_change.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            c.cookies.set("bss_portal_step_up", grant)
            resp = c.post(
                "/plan/change/cancel",
                data={"subscription_id": "SUB-001"},
            )
            assert resp.status_code == 403

    assert fake_clients.subscription.cancel_plan_change_calls == []
    rows = await _portal_actions(seed_db)
    assert rows[0].success is False
