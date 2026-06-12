"""v0.10 — COF management at /payment-methods.

V0_10_0.md Track 4 + Track 10:

* GET /payment-methods lists the customer's cards.
* GET /payment-methods/add renders the Stripe-shaped form.
* POST /payment-methods/add requires step-up; tokenizes client-side
  + one create_payment_method call.
* POST /payment-methods/<pm_id>/remove + /set-default require
  step-up + ownership; ownership failures + PolicyViolations write
  portal_action audit rows.
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


async def _setup(seed_db, customer_id="CUST-042", *, with_grant_for=None):
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


def _seed_method(fake_clients, *, pm_id, customer_id, last4="4242", is_default=False):
    fake_clients.payment.methods_by_customer.setdefault(customer_id, []).append({
        "id": pm_id,
        "customerId": customer_id,
        "brand": "visa",
        "last4": last4,
        "expMonth": 12,
        "expYear": 2030,
        "isDefault": is_default,
    })


# ── GET /payment-methods ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_renders_owned_methods(seed_db, fake_clients):
    sid, _, _ = await _setup(seed_db)
    _seed_method(fake_clients, pm_id="PM-0001", customer_id="CUST-042",
                 last4="4242", is_default=True)
    _seed_method(fake_clients, pm_id="PM-0002", customer_id="CUST-042",
                 last4="0001")

    with patch(
        "bss_self_serve.routes.payment_methods.get_clients",
        return_value=fake_clients,
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/payment-methods")
            assert resp.status_code == 200
            body = resp.text
            assert "4242" in body
            assert "0001" in body
            assert "Default" in body  # badge on PM-0001
            # Set-as-default form only renders for the non-default one.
            assert 'action="/payment-methods/PM-0002/set-default"' in body
            assert 'action="/payment-methods/PM-0001/set-default"' not in body


@pytest.mark.asyncio
async def test_list_does_not_leak_other_customers_methods(seed_db, fake_clients):
    """Bob has methods, Ada has none — Ada's listing must be empty."""
    sid, _, _ = await _setup(seed_db, customer_id="CUST-ADA")
    _seed_method(fake_clients, pm_id="PM-BOB1", customer_id="CUST-BOB", last4="9999")

    with patch(
        "bss_self_serve.routes.payment_methods.get_clients",
        return_value=fake_clients,
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.get("/payment-methods")
            assert resp.status_code == 200
            assert "9999" not in resp.text
            assert "No cards on file" in resp.text


# ── POST /payment-methods/add ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_method_requires_step_up(seed_db, fake_clients):
    sid, _, _ = await _setup(seed_db)
    with patch(
        "bss_self_serve.routes.payment_methods.get_clients",
        return_value=fake_clients,
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.post(
                "/payment-methods/add",
                data={
                    "card_number": "4242424242424242",
                    "exp_month": 12,
                    "exp_year": 2030,
                    "cvv": "123",
                    "holder_name": "Ada Lovelace",
                },
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "/auth/step-up" in resp.headers["location"]
            assert fake_clients.payment.create_calls == []


@pytest.mark.asyncio
async def test_add_method_succeeds_and_audits(seed_db, fake_clients):
    sid, _, grant = await _setup(seed_db, with_grant_for="payment_method_add")

    with patch(
        "bss_self_serve.routes.payment_methods.get_clients",
        return_value=fake_clients,
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            c.cookies.set("bss_portal_step_up", grant)
            resp = c.post(
                "/payment-methods/add",
                data={
                    "card_number": "4242424242424242",
                    "exp_month": 12,
                    "exp_year": 2030,
                    "cvv": "123",
                    "holder_name": "Ada Lovelace",
                    "postal_code": "12345",
                },
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert resp.headers["location"] == "/payment-methods?flash=added"

    # Tokenization happened client-side — token starts with tok_, last4=4242, brand=visa.
    assert len(fake_clients.payment.create_calls) == 1
    call = fake_clients.payment.create_calls[0]
    assert call["customer_id"] == "CUST-042"
    assert call["last4"] == "4242"
    assert call["brand"] == "visa"
    assert call["card_token"].startswith("tok_")

    rows = await _portal_actions(seed_db)
    assert len(rows) == 1
    assert rows[0].action == "payment_method_add"
    assert rows[0].success is True
    assert rows[0].step_up_consumed is True


@pytest.mark.asyncio
async def test_add_method_invalid_card_audits_failure(seed_db, fake_clients):
    sid, _, grant = await _setup(seed_db, with_grant_for="payment_method_add")

    with patch(
        "bss_self_serve.routes.payment_methods.get_clients",
        return_value=fake_clients,
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            c.cookies.set("bss_portal_step_up", grant)
            resp = c.post(
                "/payment-methods/add",
                data={
                    "card_number": "abcdefghijklmnop",  # not digits
                    "exp_month": 12,
                    "exp_year": 2030,
                    "cvv": "123",
                    "holder_name": "Ada",
                },
            )
            assert resp.status_code == 422
            assert "doesn&#39;t look right" in resp.text or "doesn't look right" in resp.text

    assert fake_clients.payment.create_calls == []
    rows = await _portal_actions(seed_db)
    assert len(rows) == 1
    assert rows[0].success is False
    assert rows[0].error_rule == "policy.payment.method.invalid_card"


@pytest.mark.asyncio
async def test_add_method_policy_violation_renders_customer_copy(seed_db, fake_clients):
    sid, _, grant = await _setup(seed_db, with_grant_for="payment_method_add")
    fake_clients.payment.next_error = PolicyViolationFromServer(
        rule="policy.payment.method.declined",
        message="engineer-shaped diagnostic",
    )

    with patch(
        "bss_self_serve.routes.payment_methods.get_clients",
        return_value=fake_clients,
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            c.cookies.set("bss_portal_step_up", grant)
            resp = c.post(
                "/payment-methods/add",
                data={
                    "card_number": "4242424242424242",
                    "exp_month": 12,
                    "exp_year": 2030,
                    "cvv": "123",
                    "holder_name": "Ada",
                },
            )
            assert resp.status_code == 422
            assert "declined" in resp.text
            assert "engineer-shaped diagnostic" not in resp.text

    rows = await _portal_actions(seed_db)
    assert rows[0].error_rule == "policy.payment.method.declined"


# ── POST /payment-methods/<pm_id>/remove ─────────────────────────────────


@pytest.mark.asyncio
async def test_remove_method_succeeds_for_owner(seed_db, fake_clients):
    sid, _, grant = await _setup(seed_db, with_grant_for="payment_method_remove")
    _seed_method(fake_clients, pm_id="PM-0001", customer_id="CUST-042")

    with patch(
        "bss_self_serve.routes.payment_methods.get_clients",
        return_value=fake_clients,
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            c.cookies.set("bss_portal_step_up", grant)
            resp = c.post(
                "/payment-methods/PM-0001/remove",
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert resp.headers["location"] == "/payment-methods?flash=removed"

    assert fake_clients.payment.remove_calls == ["PM-0001"]
    rows = await _portal_actions(seed_db)
    assert rows[0].action == "payment_method_remove"
    assert rows[0].success is True


@pytest.mark.asyncio
async def test_remove_method_cross_customer_returns_403_and_audits(
    seed_db, fake_clients
):
    """Bob can't remove Ada's card."""
    sid, _, grant = await _setup(
        seed_db, customer_id="CUST-BOB", with_grant_for="payment_method_remove"
    )
    _seed_method(fake_clients, pm_id="PM-ADA1", customer_id="CUST-ADA")

    with patch(
        "bss_self_serve.routes.payment_methods.get_clients",
        return_value=fake_clients,
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            c.cookies.set("bss_portal_step_up", grant)
            resp = c.post("/payment-methods/PM-ADA1/remove")
            assert resp.status_code == 403

    assert fake_clients.payment.remove_calls == []
    rows = await _portal_actions(seed_db)
    assert rows[0].success is False
    assert rows[0].error_rule == "policy.ownership.payment_method_not_owned"


@pytest.mark.asyncio
async def test_remove_method_last_with_active_line_renders_policy_error(
    seed_db, fake_clients
):
    """Server-side policy refuses; portal renders the error cleanly."""
    sid, _, grant = await _setup(seed_db, with_grant_for="payment_method_remove")
    _seed_method(fake_clients, pm_id="PM-0001", customer_id="CUST-042",
                 is_default=True)
    fake_clients.payment.next_error = PolicyViolationFromServer(
        rule="policy.payment.method.cannot_remove_last_with_active_lines",
        message="diagnostic",
    )

    with patch(
        "bss_self_serve.routes.payment_methods.get_clients",
        return_value=fake_clients,
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            c.cookies.set("bss_portal_step_up", grant)
            resp = c.post("/payment-methods/PM-0001/remove")
            assert resp.status_code == 422
            assert "active line" in resp.text

    rows = await _portal_actions(seed_db)
    assert rows[0].success is False
    assert (
        rows[0].error_rule
        == "policy.payment.method.cannot_remove_last_with_active_lines"
    )


# ── POST /payment-methods/<pm_id>/set-default ────────────────────────────


@pytest.mark.asyncio
async def test_set_default_succeeds_for_owner(seed_db, fake_clients):
    sid, _, grant = await _setup(seed_db, with_grant_for="payment_method_set_default")
    _seed_method(fake_clients, pm_id="PM-0001", customer_id="CUST-042",
                 is_default=True)
    _seed_method(fake_clients, pm_id="PM-0002", customer_id="CUST-042")

    with patch(
        "bss_self_serve.routes.payment_methods.get_clients",
        return_value=fake_clients,
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            c.cookies.set("bss_portal_step_up", grant)
            resp = c.post(
                "/payment-methods/PM-0002/set-default",
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert resp.headers["location"] == "/payment-methods?flash=default_set"

    assert fake_clients.payment.set_default_calls == ["PM-0002"]
    rows = await _portal_actions(seed_db)
    assert rows[0].action == "payment_method_set_default"
    assert rows[0].success is True


@pytest.mark.asyncio
async def test_set_default_cross_customer_returns_403(seed_db, fake_clients):
    sid, _, grant = await _setup(
        seed_db, customer_id="CUST-BOB", with_grant_for="payment_method_set_default"
    )
    _seed_method(fake_clients, pm_id="PM-ADA1", customer_id="CUST-ADA")

    with patch(
        "bss_self_serve.routes.payment_methods.get_clients",
        return_value=fake_clients,
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            c.cookies.set("bss_portal_step_up", grant)
            resp = c.post("/payment-methods/PM-ADA1/set-default")
            assert resp.status_code == 403

    assert fake_clients.payment.set_default_calls == []
    rows = await _portal_actions(seed_db)
    assert rows[0].success is False
    assert rows[0].error_rule == "policy.ownership.payment_method_not_owned"
